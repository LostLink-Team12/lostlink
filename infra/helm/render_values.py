"""Turn `terraform output -json` (infra/terraform/environments/dev) into Helm values.

Only non-secret identifiers are copied: ARNs, URLs, IDs and names. Secret values stay in
Secrets Manager and are read by each pod at startup.

    terraform -chdir=infra/terraform/environments/dev output -json > tf-outputs.json
    python infra/helm/render_values.py tf-outputs.json --image-tag <full-commit-sha> \
        --owner-app-url https://<owner-app-origin> > values-dev.generated.json
    helm upgrade --install lostlink infra/helm/lostlink -n lostlink --create-namespace \
        -f values-dev.generated.json --set-file database.caBundle=global-bundle.pem

JSON is valid YAML, so Helm accepts the output with -f. Do not commit the generated file.
"""

import argparse
import json
import re
import sys

SHA = re.compile(r"^[0-9a-f]{40}$")
ROLE = re.compile(r"^arn:aws:iam::(\d{12}):role/(.+)$")


def build(outputs: dict, image_tag: str, owner_app_url: str, ses_from: str | None) -> dict:
    def value(name):
        if name not in outputs:
            raise SystemExit(f"terraform output {name!r} is missing")
        return outputs[name]["value"]

    app = value("application")
    if not app:
        raise SystemExit("application output is null: set application_runtime_enabled = true")
    if not SHA.match(image_tag):
        raise SystemExit("--image-tag must be the full 40-character commit SHA published to ECR")
    if not owner_app_url.startswith("https://"):
        raise SystemExit("--owner-app-url must be an https:// origin")

    roles = app["runtime_role_arns"]
    sample = ROLE.match(next(iter(roles.values())))
    if not sample:
        raise SystemExit("unexpected runtime role ARN format")
    prefix_name = sample.group(2).rsplit("-", 2)[0]  # lostlink-dev-item-api -> lostlink-dev
    role_prefix = f"arn:aws:iam::{sample.group(1)}:role/{prefix_name}-"
    overrides = {k: v for k, v in roles.items() if v != f"{role_prefix}{k}"}

    secret_prefix = {
        arn.split(":secret:", 1)[1].rsplit("/database/", 1)[0]
        for arn in app["database_secret_arns"].values()
    }
    if len(secret_prefix) != 1:
        raise SystemExit("could not derive the Secrets Manager name prefix")
    registry = next(iter(value("ecr_repositories").values())).rsplit("/", 1)[0]
    machine = app["machine_client_ids"]

    return {
        "image": {"registry": registry, "tag": image_tag},
        "aws": {
            "region": value("region"),
            "roleArnPrefix": role_prefix,
            "roleArns": overrides,
            "secretPrefix": secret_prefix.pop(),
            "eventTopicArn": app["event_topic_arn"],
            "queueUrls": {k: app["queue_urls"][k] for k in ("matching", "item", "notifications")},
            "mediaBucket": value("media_bucket"),
        },
        "cognito": {
            "issuer": app["issuer"],
            "userPoolId": app["user_pool_id"],
            "clients": {"owner": app["web_client_id"], "operator": app["operator_client_id"]},
            "machineClients": {
                k: machine[k] for k in ("item", "claim", "matching", "notification")
            },
        },
        "notification": {
            "ownerAppBaseUrl": owner_app_url.rstrip("/"),
            "sesFromAddress": ses_from or outputs.get("notification_sender_email", {}).get("value"),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("outputs", type=argparse.FileType("r", encoding="utf-8"))
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--owner-app-url", required=True)
    parser.add_argument("--ses-from-address")
    args = parser.parse_args(argv)
    outputs = json.load(args.outputs)
    values = build(outputs, args.image_tag, args.owner_app_url, args.ses_from_address)
    json.dump(values, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
