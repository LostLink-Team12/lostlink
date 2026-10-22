"""Validate process configuration with the actual backend validators and Helm."""

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from values import MODES, SERVICES, helm_values

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def generated():
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


def settings_values(env):
    result = {k.lower(): v for k, v in env.items() if not k.endswith("_ARN")}
    for key in list(result):
        if key.endswith("client_ids") or key in {"item_photo_buckets", "cleanup_prefixes"}:
            result[key] = json.loads(result[key])
    for key, arn in json.loads(env["DATABASE_SECRET_ARNS"]).items():
        role = arn.replace(":", "_")
        result[key.lower()] = f"postgresql+psycopg://{role}:pw@db/lostlink"
    if "MACHINE_SECRET_ARN" in env:
        result.update(
            machine_token_url="https://cognito.example/oauth2/token",
            machine_client_id="machine",
            machine_client_secret="secret",
        )
    if "CRYPTO_SECRET_ARN" in env:
        result.update(
            field_encryption_keys='{"k":"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="}',
            field_encryption_active_key_id="k",
            handover_token_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        )
    return result


def test_every_deployed_process_passes_real_settings_validation(generated):
    for service, values in generated.items():
        module = importlib.import_module(f"{service}_app.config")
        cls = getattr(module, service.capitalize() + "Settings")
        for mode, process in values["processes"].items():
            cls(_env_file=None, **settings_values({**values["env"], **process["env"]}))
            assert process["roleArn"].endswith("/" + service + "-" + mode)


def test_background_consumers_do_not_receive_sender_or_crypto_credentials(generated):
    for service, mode in [("claim", "outbox"), ("notification", "consume")]:
        env = generated[service]["processes"][mode]["env"]
        assert not {"MACHINE_SECRET_ARN", "CRYPTO_SECRET_ARN", "DATABASE_URL"} & env.keys()
        assert len(json.loads(env["DATABASE_SECRET_ARNS"])) == 1
    assert generated["item"]["processes"]["cleanup"]["env"]["CLEANUP_PREFIXES"] == '["staging/"]'


def test_matching_api_and_tasks_pin_the_same_model(generated):
    api, tasks = (generated["matching"]["processes"][m] for m in ["api", "tasks"])
    assert api["env"]["CLIP_SHA256"] == tasks["env"]["CLIP_SHA256"]
    assert tasks["keda"] and not api["keda"]


@pytest.mark.skipif(not os.environ.get("HELM_BIN"), reason="Set HELM_BIN for template validation")
def test_rendered_charts_have_probes_isolated_roles_and_postgres_scaler(generated, tmp_path):
    helm = os.environ["HELM_BIN"]
    repositories = tmp_path / "repositories.yaml"
    repositories.write_text("apiVersion: v1\nrepositories: []\n", encoding="utf-8")
    env = {
        **os.environ,
        "HELM_REPOSITORY_CONFIG": str(repositories),
        "HELM_REPOSITORY_CACHE": str(tmp_path / "cache"),
    }
    for service, values in generated.items():
        chart = ROOT / "infra/helm" / (service + "-service")
        subprocess.run(
            [helm, "dependency", "build", str(chart), "--skip-refresh"], check=True, env=env
        )
        path = tmp_path / f"{service}.yaml"
        path.write_text(yaml.safe_dump(values), encoding="utf-8")
        rendered = subprocess.run(
            [helm, "template", service, str(chart), "-f", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        resources = list(yaml.safe_load_all(rendered))
        deployments = [r for r in resources if r and r["kind"] == "Deployment"]
        assert len(deployments) == len(MODES[service])
        for deployment in deployments:
            spec = deployment["spec"]["template"]["spec"]
            assert spec["serviceAccountName"] == deployment["metadata"]["name"]
            app = spec["containers"][0]
            assert {"startupProbe", "readinessProbe", "livenessProbe"} <= app.keys()
            assert app["securityContext"]["readOnlyRootFilesystem"]
        if service == "matching":
            scaler = next(r for r in resources if r and r["kind"] == "ScaledObject")
            trigger = scaler["spec"]["triggers"][0]
            assert trigger["type"] == "postgresql"
            assert "available_at <= now()" in trigger["metadata"]["query"]
            assert scaler["spec"]["minReplicaCount"] >= 1
