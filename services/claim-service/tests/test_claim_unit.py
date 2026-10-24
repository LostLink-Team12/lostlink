"""Configuration, token format, Item outcome mapping and HTTP guards without a database."""

import base64
import os
from uuid import uuid4

import pytest
from claim_app.clients import ItemClient, ItemRejected, ItemUnavailable
from claim_app.config import ClaimSettings
from claim_app.errors import ClaimError
from claim_app.main import create_app
from claim_app.service import parse_token, token_hash
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from pydantic import ValidationError

KEY = base64.b64encode(os.urandom(32)).decode()


def settings(**values):
    return ClaimSettings(_env_file=None, **values)


def test_deployed_configuration_requires_every_dependency():
    with pytest.raises(ValidationError):
        settings(environment="dev")
    with pytest.raises(ValidationError):
        settings(field_encryption_keys='{"k1": "x"}')
    with pytest.raises(ValidationError):
        settings(handover_token_key=base64.b64encode(b"short").decode())
    with pytest.raises(ValidationError):
        settings(item_base_url="http://item.internal")
    with pytest.raises(ValidationError):
        settings(
            database_url="postgresql+psycopg://same:x@db/lostlink",
            worker_database_url="postgresql+psycopg://same:y@db/lostlink",
        )
    assert settings(handover_token_key=KEY).token_key_bytes() == base64.b64decode(KEY)


def test_outbox_worker_starts_without_item_or_key_material():
    worker = {
        "environment": "dev",
        "process_mode": "worker",
        "worker_database_url": "postgresql+psycopg://lostlink_claim_worker:x@db/lostlink",
    }
    assert settings(**worker, worker_mode="outbox").worker_mode == "outbox"
    for mode in ["tasks", None]:
        with pytest.raises(ValidationError):
            settings(**worker, worker_mode=mode)


def test_token_format_and_keyed_hash():
    claim_id, token_id = uuid4(), uuid4()
    secret = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    assert parse_token(f"LL1.{claim_id}.{token_id}.{secret}") == (claim_id, token_id, secret)
    for value in [f"LL2.{claim_id}.{token_id}.{secret}", f"LL1.{claim_id}.{token_id}.short", ""]:
        with pytest.raises(ClaimError):
            parse_token(value)
    first = token_hash(b"k" * 32, token_id, secret)
    assert first == token_hash(b"k" * 32, token_id, secret)
    assert first != token_hash(b"x" * 32, token_id, secret)
    assert secret not in first


def test_deployed_outbox_starts_without_api_or_task_secrets():
    config = settings(
        environment="dev",
        process_mode="worker",
        worker_mode="outbox",
        worker_database_url="postgresql+psycopg://claim_worker:x@db/lostlink",
    )
    assert config.machine_client_secret is None
    assert config.field_encryption_keys is None
    with pytest.raises(ValidationError):
        settings(
            environment="dev",
            process_mode="worker",
            worker_mode="tasks",
            worker_database_url="postgresql+psycopg://claim_worker:x@db/lostlink",
        )
    with pytest.raises(ValidationError):
        settings(environment="dev", process_mode="worker", worker_mode="outbox")


def item_client(status, body):
    calls = []

    def transport(method, url, headers, payload=None):
        calls.append((method, url, headers, payload))
        if isinstance(status, Exception):
            raise status
        return status, body

    return ItemClient("https://item.internal", lambda: "machine", transport=transport), calls


def test_item_outcomes_map_to_business_rejections_or_retryable_unknowns():
    item_id, claim_id = uuid4(), uuid4()
    held = {
        "claim_id": str(claim_id),
        "item_id": str(item_id),
        "item_version": 2,
        "state": "held",
        "handover_id": None,
        "mode": None,
        "version": 5,
    }
    client, calls = item_client(200, held)
    assert client.reserve(item_id, claim_id, 2).state == "held"
    method, url, headers, payload = calls[0]
    assert (method, headers["Idempotency-Key"]) == ("POST", f"claim:{claim_id}:reserve")
    assert url == f"https://item.internal/internal/v1/items/{item_id}/reservations"
    assert payload == {"claim_id": str(claim_id), "item_version": 2}
    with pytest.raises(ItemUnavailable):
        item_client(200, {**held, "claim_id": str(uuid4())})[0].reserve(item_id, claim_id, 2)
    conflict = {"error": {"code": "item_not_available"}}
    with pytest.raises(ItemRejected) as rejected:
        item_client(409, conflict)[0].reserve(item_id, claim_id, 2)
    assert rejected.value.code == "item_not_available"
    for status, body in [
        (409, {"error": {"code": "operation_in_progress"}}),
        (503, None),
        (500, {"error": {"code": "internal_error"}}),
        (OSError("down"), None),
    ]:
        failing = item_client(status, body)[0]
        with pytest.raises(ItemUnavailable):
            failing.reserve(item_id, claim_id, 2)
    with pytest.raises(ValueError):
        ItemClient("http://item.internal", lambda: "machine")


class Verifier:
    tokens = {
        "owner": Principal(subject="owner", client_id="web", roles={"owner"}),
        "machine": Principal(subject="m2m", client_id="claim", scopes={"lostlink/x"}),
        "foreign-app": Principal(subject="owner", client_id="other", roles={"owner"}),
        "staff": Principal(
            subject="staff", client_id="web", tenant_id=uuid4(), roles={"venue_staff"}
        ),
    }

    def verify(self, token):
        if token not in self.tokens:
            raise InvalidToken()
        return self.tokens[token]


def test_probes_and_guards_fail_closed_without_dependencies():
    app = create_app(settings(user_client_ids=["web"]), token_verifier=Verifier())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        ready = client.get("/ready")
        assert ready.status_code == 503 and ready.json()["checks"]["database"] is False
        assert client.get("/v1/claims").status_code == 401
        for token in ["machine", "foreign-app", "staff"]:
            response = client.get("/v1/claims", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 403
        owner = {"Authorization": "Bearer owner"}
        assert client.get("/v1/claims", headers=owner).status_code == 503
        assert client.get("/v1/venue/claims", headers=owner).status_code == 403
        staff = {"Authorization": "Bearer staff"}
        response = client.get("/v1/venue/claims", headers=staff)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "membership_unavailable"
        assert "/internal" not in str(client.get("/openapi.json").json()["paths"])


def test_invalid_report_filter_is_rejected_before_database_access():
    app = create_app(settings(user_client_ids=["web"]), token_verifier=Verifier())
    with TestClient(app) as client:
        response = client.get(
            "/v1/claims?lost_report_id=not-a-uuid", headers={"Authorization": "Bearer owner"}
        )
        assert response.status_code == 422
