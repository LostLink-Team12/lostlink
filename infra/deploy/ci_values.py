"""Non-secret CI inputs shared by chart contract tests and security rendering."""

from values import MODES, SERVICES, helm_values


def service_values():
    secrets = {
        f"database/{s}/{mode}": f"secret:{s}:{mode}"
        for s in SERVICES
        for mode in ["api", "worker", "platform", "internal", "admin"]
    }
    secrets["claim/crypto"] = "secret:claim:crypto"
    app = {
        "web_client_id": "owner",
        "operator_client_id": "operator",
        "machine_client_ids": {s: s + "-machine" for s in SERVICES},
        "machine_secret_arns": {s: "secret:machine:" + s for s in SERVICES},
        "database_secret_arns": secrets,
        "issuer": "https://cognito.example/pool",
        "user_pool_id": "pool",
        "event_topic_arn": "arn:topic",
        "queue_urls": {
            s: "https://sqs.example/" + s for s in ["item", "matching", "notifications"]
        },
        "runtime_role_arns": {
            f"{s}-{m}": f"arn:aws:iam::123456789012:role/{s}-{m}"
            for s in SERVICES
            for m in MODES[s]
        },
    }
    outputs = {
        "application": {"value": app},
        "media_bucket": {"value": "demo-media"},
        "region": {"value": "ap-southeast-1"},
        "notification_sender_email": {"value": "sender@example.com"},
    }
    return helm_values(
        outputs,
        {s: "example/" + s + "@sha256:" + "a" * 64 for s in SERVICES},
        origin="https://frontend.example",
        revision="test",
        proxy_image="example/proxy@sha256:" + "b" * 64,
    )
