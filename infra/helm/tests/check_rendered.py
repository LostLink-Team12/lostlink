"""Check `helm template` output against the C1 application contract and backend rules.

    helm template lostlink infra/helm/lostlink -n lostlink \
        -f infra/helm/tests/values-ci.yaml > rendered.yaml
    python infra/helm/tests/check_rendered.py rendered.yaml

PROCESS_SECRETS mirrors `process_contracts` in infra/terraform/environments/dev/
application-iam.tf: each IRSA role may read only these Secrets Manager secrets, so a pod
asking for anything else would fail at startup. Keep both in step.
"""

import json
import sys
from collections import Counter

import yaml

PREFIX = "lostlink-dev/"
PROCESS_SECRETS = {
    "venue-api": {"database/venue/api", "database/venue/platform"},
    "venue-outbox": {"database/venue/worker"},
    "venue-identity": {"database/venue/worker"},
    "item-api": {"database/item/api", "database/item/internal", "machine/item"},
    "item-tasks": {"database/item/worker"},
    "item-outbox": {"database/item/worker"},
    "item-consume": {"database/item/worker"},
    "item-cleanup": {"database/item/worker"},
    "matching-api": {"database/matching/api", "database/matching/internal", "machine/matching"},
    "matching-tasks": {"database/matching/worker", "machine/matching"},
    "matching-consume": {"database/matching/worker"},
    "matching-outbox": {"database/matching/worker"},
    "claim-api": {"database/claim/api", "machine/claim", "claim/crypto"},
    "claim-tasks": {"database/claim/worker", "machine/claim", "claim/crypto"},
    "claim-outbox": {"database/claim/worker"},
    "notification-api": {"database/notification/api", "database/notification/admin"},
    "notification-consume": {"database/notification/worker"},
    "notification-tasks": {"database/notification/worker", "machine/notification"},
}
# Processes without an IRSA role (Seed 2 skeleton).
NO_ROLE = {"delivery-api"}
# Settings each process validates at startup in ENVIRONMENT=dev (see *_app/config.py and
# workers.py); values may come from plain env or LOSTLINK_SECRET_ENV.
REQUIRED_ENV = {
    "venue-api": {
        "DATABASE_URL",
        "PLATFORM_DATABASE_URL",
        "COGNITO_ISSUER",
        "USER_CLIENT_IDS",
        "PLATFORM_CLIENT_IDS",
    },
    "venue-outbox": {"WORKER_DATABASE_URL", "EVENT_TOPIC_ARN"},
    "venue-identity": {"WORKER_DATABASE_URL", "COGNITO_USER_POOL_ID"},
    "item-api": {
        "DATABASE_URL",
        "INTERNAL_DATABASE_URL",
        "COGNITO_ISSUER",
        "USER_CLIENT_IDS",
        "VENUE_BASE_URL",
        "MACHINE_TOKEN_URL",
        "MACHINE_CLIENT_ID",
        "MACHINE_CLIENT_SECRET",
        "PHOTO_BUCKET",
    },
    "item-tasks": {"WORKER_DATABASE_URL", "PHOTO_BUCKET"},
    "item-outbox": {"WORKER_DATABASE_URL", "PHOTO_BUCKET", "EVENT_TOPIC_ARN"},
    "item-consume": {"WORKER_DATABASE_URL", "PHOTO_BUCKET", "EVENT_QUEUE_URL"},
    "item-cleanup": {"WORKER_DATABASE_URL", "PHOTO_BUCKET"},
    "matching-api": {
        "DATABASE_URL",
        "INTERNAL_DATABASE_URL",
        "COGNITO_ISSUER",
        "USER_CLIENT_IDS",
        "CLIP_CHECKPOINT",
        "CLIP_SHA256",
        "ITEM_BASE_URL",
        "MACHINE_TOKEN_URL",
        "MACHINE_CLIENT_ID",
        "MACHINE_CLIENT_SECRET",
    },
    "matching-tasks": {
        "WORKER_DATABASE_URL",
        "CLIP_CHECKPOINT",
        "CLIP_SHA256",
        "ITEM_BASE_URL",
        "MACHINE_TOKEN_URL",
        "MACHINE_CLIENT_ID",
        "MACHINE_CLIENT_SECRET",
    },
    "matching-consume": {"WORKER_DATABASE_URL", "EVENT_QUEUE_URL"},
    "matching-outbox": {"WORKER_DATABASE_URL", "EVENT_TOPIC_ARN"},
    "claim-api": {
        "DATABASE_URL",
        "COGNITO_ISSUER",
        "USER_CLIENT_IDS",
        "VENUE_BASE_URL",
        "ITEM_BASE_URL",
        "MACHINE_TOKEN_URL",
        "MACHINE_CLIENT_ID",
        "MACHINE_CLIENT_SECRET",
        "COGNITO_USER_POOL_ID",
        "FIELD_ENCRYPTION_KEYS",
        "FIELD_ENCRYPTION_ACTIVE_KEY_ID",
        "HANDOVER_TOKEN_KEY",
    },
    "claim-tasks": {
        "WORKER_DATABASE_URL",
        "ITEM_BASE_URL",
        "MACHINE_TOKEN_URL",
        "MACHINE_CLIENT_ID",
        "MACHINE_CLIENT_SECRET",
        "FIELD_ENCRYPTION_KEYS",
        "FIELD_ENCRYPTION_ACTIVE_KEY_ID",
        "HANDOVER_TOKEN_KEY",
    },
    "claim-outbox": {"WORKER_DATABASE_URL", "EVENT_TOPIC_ARN"},
    "notification-api": {"DATABASE_URL", "COGNITO_ISSUER", "USER_CLIENT_IDS"},
    "notification-consume": {"WORKER_DATABASE_URL", "EVENT_QUEUE_URL"},
    "notification-tasks": {
        "WORKER_DATABASE_URL",
        "MATCHING_BASE_URL",
        "MACHINE_TOKEN_URL",
        "MACHINE_CLIENT_ID",
        "MACHINE_CLIENT_SECRET",
        "COGNITO_USER_POOL_ID",
        "OWNER_APP_BASE_URL",
        "SES_FROM_ADDRESS",
    },
}
PRIVATE_PATHS = ("/internal", "/metrics", "/health", "/ready")


def main(path):
    docs = [d for d in yaml.safe_load_all(open(path, encoding="utf-8")) if d]
    by_kind = Counter(d["kind"] for d in docs)
    errors = []

    def objects(kind):
        return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}

    deployments, accounts = objects("Deployment"), objects("ServiceAccount")
    services, scaled = objects("Service"), objects("ScaledObject")
    hpas = objects("HorizontalPodAutoscaler")

    unexpected = set(deployments) - set(PROCESS_SECRETS) - NO_ROLE
    missing = set(PROCESS_SECRETS) - set(deployments)
    if unexpected or missing:
        errors.append(f"processes missing {sorted(missing)}, unexpected {sorted(unexpected)}")

    for name, deployment in deployments.items():
        pod = deployment["spec"]["template"]["spec"]
        if pod["serviceAccountName"] != name or name not in accounts:
            errors.append(f"{name}: service account must be named after the process")
        role = accounts.get(name, {}).get("metadata", {}).get("annotations", {})
        role = role.get("eks.amazonaws.com/role-arn")
        if name in NO_ROLE:
            if role:
                errors.append(f"{name}: unexpected IRSA role")
        elif role != f"arn:aws:iam::152174417545:role/lostlink-dev-{name}":
            errors.append(f"{name}: IRSA role {role}")
        container = pod["containers"][0]
        env = {e["name"]: e.get("value") for e in container.get("env", [])}
        secret_env = json.loads(env.get("LOSTLINK_SECRET_ENV") or "{}")
        secrets = {spec["secret"].removeprefix(PREFIX) for spec in secret_env.values()}
        allowed = PROCESS_SECRETS.get(name, set())
        if not secrets <= allowed:
            errors.append(f"{name}: reads {sorted(secrets - allowed)} outside its IAM policy")
        if any("migration" in s for s in secrets) or "MIGRATION_DATABASE_URL" in env:
            errors.append(f"{name}: migration credential injected into a runtime pod")
        if set(env) & set(secret_env):
            errors.append(f"{name}: {sorted(set(env) & set(secret_env))} set twice")
        missing = REQUIRED_ENV.get(name, set()) - set(env) - set(secret_env)
        if missing:
            errors.append(f"{name}: missing {sorted(missing)}")
        for key, value in env.items():
            if key.endswith("_BASE_URL") and not str(value).startswith("https://"):
                errors.append(f"{name}: {key} must be HTTPS")
            if value in (None, ""):
                errors.append(f"{name}: {key} is empty")
        if env.get("ENVIRONMENT") not in {"dev", "prod"}:
            errors.append(f"{name}: ENVIRONMENT must be dev or prod")
        security = container.get("securityContext", {})
        if security.get("allowPrivilegeEscalation") is not False:
            errors.append(f"{name}: privilege escalation allowed")
        if not pod.get("securityContext", {}).get("runAsNonRoot"):
            errors.append(f"{name}: must run as non-root")
        if "secret_env" not in " ".join(container["command"]):
            errors.append(f"{name}: not started through lostlink_common.secret_env")
        if "memory" not in container.get("resources", {}).get("limits", {}):
            errors.append(f"{name}: memory limit missing")
        managed = name in hpas or name in scaled
        if managed == ("replicas" in deployment["spec"]):
            errors.append(f"{name}: replicas must be omitted exactly when autoscaled")

    for name, service in services.items():
        process = name.removesuffix("-service") + "-api"
        selector = service["spec"]["selector"]
        labels = deployments.get(process, {}).get("spec", {}).get("template", {})
        labels = labels.get("metadata", {}).get("labels", {})
        if not selector or any(labels.get(k) != v for k, v in selector.items()):
            errors.append(f"{name}: selector does not match {process}")

    for name, obj in scaled.items():
        if obj["spec"]["scaleTargetRef"]["name"] not in deployments:
            errors.append(f"{name}: KEDA target missing")
        trigger = obj["spec"]["triggers"][0]
        query = trigger["metadata"]["query"]
        if trigger["type"] != "postgresql" or "matching.work_jobs" not in query:
            errors.append(f"{name}: KEDA must scale on the PostgreSQL backlog (ADR-0002)")
    if "matching-tasks" not in scaled:
        errors.append("matching-tasks has no KEDA ScaledObject")

    for ingress in objects("Ingress").values():
        for rule in ingress["spec"]["rules"]:
            for route in rule["http"]["paths"]:
                if route["path"].startswith(PRIVATE_PATHS) or not route["path"].startswith("/v1/"):
                    errors.append(f"ingress exposes {route['path']}")
        paths = [r["path"] for rule in ingress["spec"]["rules"] for r in rule["http"]["paths"]]
        if len(paths) != len(set(paths)):
            errors.append("ingress has duplicate paths")

    print(dict(sorted(by_kind.items())))
    if errors:
        print("\n".join(f"FAIL {e}" for e in errors))
        return 1
    print(f"OK: {len(deployments)} processes match the C1 IAM contract and startup settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
