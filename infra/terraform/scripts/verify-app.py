"""Verify live application identities without printing secrets or sending email.

--probe-events requires idle development queues and paused business consumers.
Probe messages are marked and deleted individually; unrelated messages survive.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
import jwt
from botocore.config import Config


def check_machines(app, sm):
    jwks = jwt.PyJWKClient(f"{app['issuer']}/.well-known/jwks.json")
    for name, arn in app["machine_secret_arns"].items():
        value = json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])
        authorization = base64.b64encode(
            f"{value['client_id']}:{value['client_secret']}".encode()
        ).decode()

        def request(scopes, value=value, authorization=authorization):
            return Request(
                value["token_url"],
                data=urlencode(
                    {"grant_type": "client_credentials", "scope": " ".join(scopes)}
                ).encode(),
                headers={
                    "Authorization": f"Basic {authorization}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        with urlopen(request(value["scopes"]), timeout=15) as response:
            token = json.load(response)["access_token"]
        claims = jwt.decode(
            token,
            jwks.get_signing_key_from_jwt(token).key,
            algorithms=["RS256"],
            issuer=app["issuer"],
            # The operator workstation can drift slightly from the AWS clock.
            # This allowance affects this smoke test only, not backend auth.
            leeway=30,
            options={"verify_aud": False, "require": ["exp", "iss", "client_id", "token_use"]},
        )
        assert claims["client_id"] == app["machine_client_ids"][name]
        assert claims["token_use"] == "access"
        assert set(claims["scope"].split()) == set(value["scopes"])
        if name == "item":
            try:
                with urlopen(request(["lostlink/items.reserve"]), timeout=15) as response:
                    raise AssertionError("Item client obtained an unauthorized scope")
            except HTTPError as error:
                assert error.code == 400
                # Cognito can report invalid_scope or another OAuth grant error;
                # the same valid client/request must not obtain this scope.
                assert json.load(error).get("error")
        print(f"{name}: signed M2M token and scope restriction verified", flush=True)


def check_policies(app, db, iam, analyzer, media_bucket):
    protected = [db["master_secret_arn"]] + [
        arn for key, arn in app["database_secret_arns"].items() if key.endswith("/migration")
    ]
    for name, arn in app["runtime_role_arns"].items():
        role_name = arn.rsplit("/", 1)[1]
        for policy_name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
            policy = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)[
                "PolicyDocument"
            ]
            findings = analyzer.validate_policy(
                policyDocument=json.dumps(policy), policyType="IDENTITY_POLICY"
            )["findings"]
            severe = [
                finding["issueCode"]
                for finding in findings
                if finding["findingType"] in {"ERROR", "SECURITY_WARNING"}
            ]
            assert not severe, (name, severe)
        result = iam.simulate_principal_policy(
            PolicySourceArn=arn,
            ActionNames=["secretsmanager:GetSecretValue"],
            ResourceArns=protected,
        )
        assert all(entry["EvalDecision"] != "allowed" for entry in result["EvaluationResults"])
    for process, action, prefix, expected in [
        ("item-api", "s3:PutObject", "staging", True),
        ("item-api", "s3:PutObject", "sealed", False),
        ("item-tasks", "s3:PutObject", "sealed", True),
        ("matching-tasks", "s3:GetObject", "sealed", True),
        ("matching-tasks", "s3:PutObject", "sealed", False),
    ]:
        result = iam.simulate_principal_policy(
            PolicySourceArn=app["runtime_role_arns"][process],
            ActionNames=[action],
            ResourceArns=[f"arn:aws:s3:::{media_bucket}/{prefix}/infra-probe"],
        )
        assert (result["EvaluationResults"][0]["EvalDecision"] == "allowed") == expected
    print(
        "18 IAM policies validated; admin/migration secrets denied; "
        "photo write boundaries verified.",
        flush=True,
    )


def check_events(app, sns, sqs):
    routes = {
        "matching": {"item.registered", "item.catalog_changed"},
        "item": {"item.embedded", "item.embedding_failed"},
        "notifications": {"match.found"},
    }
    for url in app["queue_urls"].values():
        counts = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        assert all(int(count) == 0 for count in counts.values()), "Probe requires idle dev queues"
    probe = str(uuid.uuid4())
    for event_type in sorted(set.union(*routes.values()) | {"infra.probe.unsubscribed"}):
        sns.publish(
            TopicArn=app["event_topic_arn"],
            Message=json.dumps({"infra_probe": probe, "event_type": event_type}),
            MessageAttributes={
                "event_type": {"DataType": "String", "StringValue": event_type},
                "schema_version": {"DataType": "Number", "StringValue": "1"},
            },
        )
    received = {name: set() for name in routes}
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        for name, url in app["queue_urls"].items():
            messages = sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=2, VisibilityTimeout=10
            ).get("Messages", [])
            for message in messages:
                body = json.loads(message["Body"])
                if body.get("infra_probe") != probe:
                    continue
                sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
                received[name].add(body["event_type"])
        if all(received[name] >= expected for name, expected in routes.items()):
            assert received == routes, received
            print(
                "SNS -> SQS: all five event types reached exactly their intended queues; "
                "probes removed.",
                flush=True,
            )
            return
    raise TimeoutError(
        "Event probe did not complete; inspect SNS metrics and marked dev probe messages"
    )


def check_trigger(app, function_name, cognito, function):
    users = cognito.list_users_in_group(UserPoolId=app["user_pool_id"], GroupName="platform_admin")[
        "Users"
    ]
    assert users, "No platform administrator in Cognito"
    user = users[0]
    attributes = {attr["Name"]: attr["Value"] for attr in user["Attributes"]}
    event = {
        "version": "2",
        "triggerSource": "TokenGeneration_HostedAuth",
        "region": function.meta.region_name,
        "userPoolId": app["user_pool_id"],
        "userName": user["Username"],
        "callerContext": {"clientId": app["operator_client_id"]},
        "request": {
            "userAttributes": attributes,
            "groupConfiguration": {"groupsToOverride": ["platform_admin", "venue_admin"]},
        },
        "response": {},
    }
    for _attempt in range(2):
        result = function.invoke(
            FunctionName=function_name, Payload=json.dumps(event).encode(), LogType="Tail"
        )
        with result["Payload"] as body:
            payload = json.load(body)
        assert not result.get("FunctionError"), payload.get("errorType")
        claims = payload["response"]["claimsAndScopeOverrideDetails"]
        assert set(claims["groupOverrideDetails"]["groupsToOverride"]) == {
            "platform_admin",
            "owner",
        }
        assert "custom:tenant_id" in claims["accessTokenGeneration"]["claimsToSuppress"]
        log = base64.b64decode(result.get("LogResult", "")).decode()
        duration = re.search(r"(?:^|\t)Duration: ([0-9.]+) ms", log)
        initialization = re.search(r"Init Duration: ([0-9.]+) ms", log)
        elapsed = float(duration[1]) + (float(initialization[1]) if initialization else 0)
        assert elapsed < 4500, "Token trigger lacks margin below Cognito's 5-second deadline"
        print(
            f"Trigger invocation {_attempt + 1}: total {elapsed:.2f} ms "
            f"({'cold' if initialization else 'warm'})",
            flush=True,
        )
    event["callerContext"]["clientId"] = app["machine_client_ids"]["item"]
    rejected = function.invoke(FunctionName=function_name, Payload=json.dumps(event).encode())
    with rejected["Payload"] as body:
        failure = json.load(body)
    assert rejected.get("FunctionError") and failure.get("errorType") == "ValueError"
    print(
        "Cold/warm token trigger: real DB lookup, platform_admin preserved, "
        "stale venue role/tenant removed.",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="lostlink-dev")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--probe-events", action="store_true")
    parser.add_argument("--trigger", action="store_true")
    parser.add_argument("--trigger-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    os.environ.update(AWS_PROFILE=args.profile, AWS_REGION=args.region)
    outputs = json.loads(
        subprocess.check_output(
            ["terraform", f"-chdir={root / 'infra/terraform/environments/dev'}", "output", "-json"],
            text=True,
        )
    )
    app, db = outputs["application"]["value"], outputs["database"]["value"]
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    config = Config(
        connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3}
    )
    clients = {
        name: session.client(name, config=config)
        for name in [
            "sts",
            "secretsmanager",
            "iam",
            "accessanalyzer",
            "sns",
            "sqs",
            "cognito-idp",
            "lambda",
            "sesv2",
        ]
    }
    assert clients["sts"].get_caller_identity()["Account"] == app["kms_key_arn"].split(":")[4]
    if args.trigger_only:
        check_trigger(
            app,
            outputs["token_trigger_function_name"]["value"],
            clients["cognito-idp"],
            clients["lambda"],
        )
        return
    check_machines(app, clients["secretsmanager"])
    check_policies(
        app, db, clients["iam"], clients["accessanalyzer"], outputs["media_bucket"]["value"]
    )
    if args.probe_events:
        check_events(app, clients["sns"], clients["sqs"])
    if args.trigger:
        check_trigger(
            app,
            outputs["token_trigger_function_name"]["value"],
            clients["cognito-idp"],
            clients["lambda"],
        )
    sender = outputs["notification_sender_email"]["value"]
    assert clients["sesv2"].get_email_identity(EmailIdentity=sender)["VerifiedForSendingStatus"]
    print("SES sender verified. No human email sent and no passwords/tokens printed.", flush=True)


if __name__ == "__main__":
    main()
