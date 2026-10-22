"""Generate public Helm inputs from Terraform's application contract.

This module never retrieves credentials. Every secret ARN is process specific.
"""

import json
from pathlib import Path

import yaml

MATCHING_DEFAULTS = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "helm/lostlink/values.yaml").read_text(encoding="utf-8")
)["matching"]

SERVICES = ["venue", "item", "matching", "claim", "notification"]
MODES = {
    "venue": ["api", "outbox", "identity"],
    "item": ["api", "tasks", "outbox", "consume", "cleanup"],
    "matching": ["api", "tasks", "consume", "outbox"],
    "claim": ["api", "tasks", "outbox"],
    "notification": ["api", "consume", "tasks"],
}
BACKLOG_QUERY = (
    "SELECT count(*) FROM matching.work_jobs "
    "WHERE state = 'pending' AND kind = 'embed' AND available_at <= now()"
)


def helm_values(outputs, images, *, origin, revision, proxy_image):
    app = outputs["application"]["value"]
    bucket = outputs["media_bucket"]["value"]
    owner, operator = app["web_client_id"], app["operator_client_id"]
    machines = app["machine_client_ids"]
    lists = {
        "venue": {
            "USER_CLIENT_IDS": [owner, operator],
            "PLATFORM_CLIENT_IDS": [operator],
            "MEMBER_CHECK_CLIENT_IDS": [machines["item"], machines["claim"]],
        },
        "item": {
            "USER_CLIENT_IDS": [owner, operator],
            "CLAIM_CLIENT_IDS": [machines["claim"]],
            "INDEX_CLIENT_IDS": [machines["matching"]],
        },
        "matching": {
            "USER_CLIENT_IDS": [owner],
            "NOTIFICATION_CLIENT_IDS": [machines["notification"]],
        },
        "claim": {"USER_CLIENT_IDS": [owner, operator]},
        "notification": {"USER_CLIENT_IDS": [owner], "OPERATOR_CLIENT_IDS": [operator]},
    }
    result = {}
    for service in SERVICES:
        common = {
            "ENVIRONMENT": "dev",
            "AWS_REGION": outputs["region"]["value"],
            "AWS_DEFAULT_REGION": outputs["region"]["value"],
            "DB_POOL_SIZE": "1",
            "DB_MAX_OVERFLOW": "0",
            "JWT_LEEWAY_SECONDS": "30",
            "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        }
        if service == "item":
            common["PHOTO_BUCKET"] = bucket
        processes = {}
        for mode in MODES[service]:
            if service == "notification" and mode == "tasks" and not origin:
                continue
            name = f"{service}-{mode}"
            env = {"PROCESS_MODE": "api" if mode == "api" else "worker"}
            db = {
                "DATABASE_URL" if mode == "api" else "WORKER_DATABASE_URL": app[
                    "database_secret_arns"
                ][f"database/{service}/{'api' if mode == 'api' else 'worker'}"]
            }
            if mode == "api":
                extra = {
                    "venue": "platform",
                    "item": "internal",
                    "matching": "internal",
                    "notification": "admin",
                }.get(service)
                if extra:
                    db[extra.upper() + "_DATABASE_URL"] = app["database_secret_arns"][
                        f"database/{service}/{extra}"
                    ]
                env.update({key: json.dumps(value) for key, value in lists[service].items()})
                clients = sorted({x for values in lists[service].values() for x in values})
                env.update(COGNITO_ISSUER=app["issuer"], COGNITO_CLIENT_IDS=json.dumps(clients))
            env["DATABASE_SECRET_ARNS"] = json.dumps(db)
            if service in {"matching", "claim", "notification"}:
                env["WORKER_MODE"] = mode if mode != "api" else "tasks"
            machine = (service in {"item", "matching", "claim"} and mode == "api") or (
                service in {"matching", "claim", "notification"} and mode == "tasks"
            )
            if machine:
                env["MACHINE_SECRET_ARN"] = app["machine_secret_arns"][service]
                if service in {"item", "claim"}:
                    env["VENUE_BASE_URL"] = "https://venue-api.lostlink.svc.cluster.local"
                if service in {"matching", "claim"}:
                    env["ITEM_BASE_URL"] = "https://item-api.lostlink.svc.cluster.local"
                if service == "notification":
                    env["MATCHING_BASE_URL"] = "https://matching-api.lostlink.svc.cluster.local"
            if service == "claim" and mode in {"api", "tasks"}:
                env["CRYPTO_SECRET_ARN"] = app["database_secret_arns"]["claim/crypto"]
            if (
                (service == "venue" and mode == "identity")
                or (service == "claim" and mode in {"api", "tasks"})
                or (service == "notification" and mode == "tasks")
            ):
                env["COGNITO_USER_POOL_ID"] = app["user_pool_id"]
            if mode == "outbox":
                env["EVENT_TOPIC_ARN"] = app["event_topic_arn"]
            if mode == "consume":
                env["EVENT_QUEUE_URL"] = app["queue_urls"][
                    "notifications" if service == "notification" else service
                ]
            if service == "item" and mode == "cleanup":
                env["CLEANUP_PREFIXES"] = '["staging/"]'
            model = service == "matching" and mode in {"api", "tasks"}
            if service == "matching":
                env.update(
                    MATCH_THRESHOLD=str(MATCHING_DEFAULTS["threshold"]),
                    MATCH_THRESHOLD_TEXT=str(MATCHING_DEFAULTS["thresholdText"]),
                )
            if model:
                env.update(
                    CLIP_CHECKPOINT="/models/ViT-B-32.pt",
                    CLIP_SHA256="40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af",
                    ITEM_PHOTO_BUCKETS=json.dumps([bucket]),
                )
            if service == "notification" and mode == "tasks":
                env.update(
                    EMAIL_SENDER="ses",
                    SES_FROM_ADDRESS=outputs["notification_sender_email"]["value"],
                    OWNER_APP_BASE_URL=origin,
                )
            command = (
                [
                    "uvicorn",
                    f"{service}_app.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                    "--no-access-log",
                ]
                if mode == "api"
                else ["python", "-m", f"{service}_app.workers", mode]
            )
            processes[mode] = {
                "roleArn": app["runtime_role_arns"][name],
                "env": env,
                "command": command,
                "model": model,
                "keda": service == "matching" and mode == "tasks",
                "resources": {
                    "requests": {
                        "cpu": "500m"
                        if model
                        else "100m"
                        if service == "item" and mode == "api"
                        else "25m",
                        "memory": "4Gi" if model else "128Mi",
                    },
                    "limits": {
                        "cpu": "1500m"
                        if model
                        else "1000m"
                        if service == "item" and mode == "api"
                        else "500m",
                        "memory": "4Gi" if model else "384Mi",
                    },
                },
            }
        result[service] = {
            "service": service,
            "image": images[service],
            "proxyImage": proxy_image,
            "runtimeRevision": revision,
            "env": common,
            "processes": processes,
            "hpa": {"maxReplicas": 8 if service == "item" else 2},
            "keda": {"maxReplicas": 2},
            "migration": {"enabled": False},
        }
    return result
