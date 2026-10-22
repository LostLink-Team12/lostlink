"""Input/auth failures and vector invariants require no external infrastructure."""

import base64
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from lostlink_common.auth import InvalidToken, Principal
from lostlink_common.membership import MembershipUnavailable
from matching_app.clients import ItemClient
from matching_app.config import MatchingSettings
from matching_app.embedding import ClipEncoder, EmbeddingError, decode_photo, normalize
from matching_app.main import create_app
from matching_app.schemas import NewReport, Search
from pydantic import ValidationError


@pytest.mark.parametrize("value", [[], [0.0] * 512, [float("nan")] * 512, [1.0] * 511])
def test_vectors_reject_wrong_dimensions_or_nonfinite_or_zero(value):
    with pytest.raises(EmbeddingError):
        normalize(value)


def test_unit_norm_and_base64():
    assert sum(x * x for x in normalize([2.0] + [0.0] * 511)) == 1
    assert decode_photo(base64.b64encode(b"image")) == b"image"
    with pytest.raises(EmbeddingError):
        decode_photo("data:image/png;base64,invalid")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"text": "   "},
        {"text": "red", "embedding": [1]},
        {"text": "red", "owner_id": "other"},
        {"text": "red", "found_from": "2026-10-20"},
        {
            "text": "red",
            "found_from": "2026-10-20T00:00:00Z",
            "found_until": "2026-10-19T00:00:00Z",
        },
    ],
)
def test_search_contract(body):
    with pytest.raises(ValidationError):
        Search.model_validate(body)


def test_report_cannot_set_owner_or_state():
    with pytest.raises(ValidationError):
        NewReport(description="bag", owner_id="other")


def test_missing_checkpoint_never_returns_fake_vectors(tmp_path):
    encoder = ClipEncoder(str(tmp_path / "missing.pt"), "0" * 64)
    with pytest.raises(EmbeddingError):
        encoder.encode("red backpack")


def test_machine_client_fixed_url_and_item_identity():
    with pytest.raises(MembershipUnavailable):
        ItemClient("http://untrusted.invalid", lambda: "secret")
    client = ItemClient(
        "https://item.invalid", lambda: "secret", transport=lambda *_: {"item_id": str(uuid4())}
    )
    with pytest.raises(MembershipUnavailable):
        client.context(uuid4())


def test_configuration_separates_clients_and_database_roles():
    with pytest.raises(ValidationError):
        MatchingSettings(_env_file=None, user_client_ids=["same"], notification_client_ids=["same"])
    with pytest.raises(ValidationError):
        MatchingSettings(
            _env_file=None,
            database_url="postgresql+psycopg://same@localhost/db",
            internal_database_url="postgresql+psycopg://same@localhost/db",
        )
    with pytest.raises(ValidationError):
        MatchingSettings(_env_file=None, clip_checkpoint="local.pt", clip_sha256=None)


def test_production_queue_worker_needs_no_api_database_or_model(monkeypatch):
    for key in [
        "CLIP_CHECKPOINT",
        "CLIP_SHA256",
        "DATABASE_URL",
        "COGNITO_ISSUER",
        "COGNITO_CLIENT_IDS",
    ]:
        monkeypatch.delenv(key, raising=False)
    settings = MatchingSettings(
        _env_file=None,
        environment="prod",
        process_mode="worker",
        worker_mode="consume",
        worker_database_url="postgresql+psycopg://worker:secret@localhost/db",
    )
    assert settings.database_url is None and settings.clip_checkpoint is None
    with pytest.raises(ValidationError):
        MatchingSettings(
            _env_file=None,
            environment="prod",
            process_mode="worker",
            worker_mode="tasks",
            worker_database_url="postgresql+psycopg://worker:secret@localhost/db",
        )


def test_request_body_limit_covers_declared_and_chunked_payloads():
    with TestClient(create_app(MatchingSettings(_env_file=None))) as client:
        response = client.post(
            "/v1/search",
            content=b"{}",
            headers={
                "Content-Length": str(9 * 1024 * 1024),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 413

        def stream():
            yield b'{"text":"'
            yield b"x" * (8 * 1024 * 1024)
            yield b'"}'

        response = client.post(
            "/v1/search", content=stream(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413, response.text


def test_http_readiness_authentication_and_role_boundaries():
    class Verifier:
        def verify(self, token):
            if token == "owner":
                return Principal(subject="a", client_id="web", roles={"owner"})
            if token == "staff":
                return Principal(subject="b", client_id="web", roles={"venue_staff"})
            raise InvalidToken()

    with TestClient(
        create_app(
            MatchingSettings(_env_file=None, user_client_ids=["web"]), token_verifier=Verifier()
        )
    ) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.post("/v1/search", json={"text": "bag"}).status_code == 401
        assert (
            client.post(
                "/v1/search", json={"text": "bag"}, headers={"Authorization": "Bearer staff"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/search", json={"text": "bag"}, headers={"Authorization": "Bearer owner"}
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/v1/search",
                json={"text": "bag", "owner_id": "other"},
                headers={"Authorization": "Bearer owner"},
            ).status_code
            == 422
        )


def test_automatic_match_threshold_depends_on_report_photo():
    from types import SimpleNamespace

    from matching_app.workers import MatchingWorker

    settings = MatchingSettings(_env_file=None, match_threshold=0.78, match_threshold_text=0.63)
    worker = MatchingWorker(None, settings=settings)
    assert worker.threshold_for(SimpleNamespace(has_photo=True)) == 0.78
    assert worker.threshold_for(SimpleNamespace(has_photo=False)) == 0.63
    defaults = MatchingSettings(_env_file=None)
    assert (defaults.match_threshold, defaults.match_threshold_text) == (0.78, 0.63)
    with pytest.raises(ValidationError):
        MatchingSettings(_env_file=None, match_threshold_text=1.5)
