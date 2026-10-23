"""Initialize AWS application secrets and migrate a private RDS through EKS Jobs.

Run with Python 3.12 and the repository's pinned boto3. Passwords stay in memory,
Secrets Manager, and short-lived Kubernetes Secrets, never files/arguments/logs.
Existing secrets are reused; this is not a password/key rotation tool.
"""

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[3]


def command(args, *, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True, check=False)
    if result.returncode:
        # kubectl errors can echo submitted objects; never include stdin in errors.
        raise RuntimeError(f"Command failed: {args[0]} {args[1]}")
    return result.stdout


def database_url(value):
    return (
        f"postgresql+psycopg://{quote(value['username'], safe='')}:"
        f"{quote(value['password'], safe='')}@{value['host']}:{value['port']}/{value['dbname']}"
        "?sslmode=verify-full&sslrootcert=/bootstrap/global-bundle.pem&connect_timeout=10"
    )


def ensure_secret(client, arn, factory):
    try:
        return json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    except client.exceptions.ResourceNotFoundException:
        value = factory()
        client.put_secret_value(
            SecretId=arn, ClientRequestToken=str(uuid.uuid4()), SecretString=json.dumps(value)
        )
        return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="lostlink-dev")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--kubectl", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--image-sha", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.image_sha):
        raise ValueError("Use a published full commit SHA, never a mutable latest tag")
    os.environ.update(
        AWS_PROFILE=args.profile, AWS_REGION=args.region, AWS_DEFAULT_REGION=args.region
    )
    outputs = json.loads(
        command(
            ["terraform", f"-chdir={ROOT / 'infra/terraform/environments/dev'}", "output", "-json"]
        )
    )
    app, db = outputs["application"]["value"], outputs["database"]["value"]
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    config = Config(
        connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3}
    )
    identity = session.client("sts", config=config).get_caller_identity()
    account = app["kms_key_arn"].split(":")[4]
    if identity["Account"] != account or ":user/" not in identity["Arn"]:
        raise RuntimeError("Use this account's non-root IAM deployment identity")
    sm = session.client("secretsmanager", config=config)
    ecr = session.client("ecr", config=config)
    # Resolve image digests before transferring any administrator credentials.
    images = {}
    for service in ["venue", "item", "matching", "claim", "notification"]:
        repo = f"lostlink/{service}-service"
        image = ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": args.image_sha}])[
            "imageDetails"
        ][0]
        images[service] = (
            f"{account}.dkr.ecr.{args.region}.amazonaws.com/{repo}@{image['imageDigest']}"
        )
    credentials = {}
    for key, username in app["database_roles"].items():
        arn = app["database_secret_arns"][f"database/{key}"]
        value = ensure_secret(
            sm,
            arn,
            lambda username=username: {
                "username": username,
                "password": secrets.token_urlsafe(36),
                "host": db["host"],
                "port": db["port"],
                "dbname": db["name"],
            },
        )
        if any(
            value[field] != expected
            for field, expected in {
                "username": username,
                "host": db["host"],
                "dbname": db["name"],
            }.items()
        ):
            raise RuntimeError(f"Existing secret metadata mismatch: {key}")
        credentials[key] = value
    ensure_secret(
        sm,
        app["database_secret_arns"]["claim/crypto"],
        lambda: {
            "FIELD_ENCRYPTION_KEYS": json.dumps(
                {"demo-v1": base64.b64encode(secrets.token_bytes(32)).decode()}
            ),
            "FIELD_ENCRYPTION_ACTIVE_KEY_ID": "demo-v1",
            "HANDOVER_TOKEN_KEY": base64.b64encode(secrets.token_bytes(48)).decode(),
        },
    )
    master = json.loads(sm.get_secret_value(SecretId=db["master_secret_arn"])["SecretString"])
    master.update(host=db["host"], port=db["port"], dbname=db["name"])
    with urlopen(
        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem", timeout=30
    ) as response:
        ca = response.read().decode()
    if "BEGIN CERTIFICATE" not in ca:
        raise RuntimeError("Invalid RDS CA bundle")
    namespace = f"lostlink-bootstrap-{uuid.uuid4().hex[:10]}"
    kube = [args.kubectl, "--kubeconfig", args.kubeconfig]

    def create(obj):
        command([*kube, "create", "-f", "-"], data=json.dumps(obj))

    def secret(name, values):
        create(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "type": "Opaque",
                "stringData": values,
            }
        )

    def job(name, image, secret_name, entrypoint):
        create(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 600,
                    "ttlSecondsAfterFinished": 600,
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "automountServiceAccountToken": False,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "bootstrap",
                                    "image": image,
                                    "command": entrypoint,
                                    "envFrom": [{"secretRef": {"name": secret_name}}],
                                    "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "128Mi"},
                                        "limits": {"cpu": "500m", "memory": "512Mi"},
                                    },
                                    "volumeMounts": [
                                        {
                                            "name": "bootstrap",
                                            "mountPath": "/bootstrap",
                                            "readOnly": True,
                                        }
                                    ],
                                }
                            ],
                            "volumes": [{"name": "bootstrap", "configMap": {"name": "bootstrap"}}],
                        }
                    },
                },
            }
        )
        deadline = time.monotonic() + 620
        while time.monotonic() < deadline:
            status = json.loads(
                command([*kube, "-n", namespace, "get", "job", name, "-o", "json"])
            )["status"]
            if status.get("succeeded"):
                print(command([*kube, "-n", namespace, "logs", f"job/{name}"]).strip(), flush=True)
                return
            if status.get("failed"):
                # Bootstrap errors are deliberately sanitized by bootstrap-db.py.
                print(command([*kube, "-n", namespace, "logs", f"job/{name}"]).strip(), flush=True)
                raise RuntimeError(f"Job failed: {name}")
            time.sleep(5)
        raise TimeoutError(f"Job timed out: {name}; inspect pod status without dumping secrets")

    created = False
    admin_ready = False
    try:
        create(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
                },
            }
        )
        created = True
        create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "bootstrap", "namespace": namespace},
                "data": {
                    "global-bundle.pem": ca,
                    "bootstrap-db.py": (Path(__file__).with_name("bootstrap-db.py")).read_text(),
                },
            }
        )
        secret(
            "admin", {"BOOTSTRAP_CREDENTIALS": json.dumps({"master": master, "roles": credentials})}
        )
        admin_ready = True
        job(
            "initialize-roles",
            images["venue"],
            "admin",
            ["python", "/bootstrap/bootstrap-db.py", "initialize"],
        )
        for service in images:
            secret(
                f"migrate-{service}",
                {"MIGRATION_DATABASE_URL": database_url(credentials[f"{service}/migration"])},
            )
            job(
                f"migrate-{service}",
                images[service],
                f"migrate-{service}",
                ["alembic", "upgrade", "head"],
            )
            print(f"{service}: migration completed", flush=True)
        job(
            "verify-isolation",
            images["venue"],
            "admin",
            ["python", "/bootstrap/bootstrap-db.py", "verify"],
        )
        print(
            "Database migrations and runtime-role isolation verified; "
            "cleaning temporary credentials.",
            flush=True,
        )
    finally:
        if created:
            try:
                if admin_ready:
                    job(
                        "revoke-migration-create",
                        images["venue"],
                        "admin",
                        ["python", "/bootstrap/bootstrap-db.py", "finalize"],
                    )
            finally:
                command([*kube, "delete", "namespace", namespace, "--wait=true", "--timeout=60s"])


if __name__ == "__main__":
    main()
