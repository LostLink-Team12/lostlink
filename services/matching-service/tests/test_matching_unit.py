"""Input/auth failures and vector invariants require no external infrastructure."""

import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
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


@pytest.mark.parametrize("has_photo,expected", [(False, True), (True, False)])
def test_rematching_applies_the_calibrated_cutoff_to_each_report_modality(
    monkeypatch, has_photo, expected
):
    import matching_app.workers as workers

    item = SimpleNamespace(item_id=uuid4(), state="available", item_version=1, tenant_id=uuid4())
    vector = SimpleNamespace(item_version=1, tenant_id=item.tenant_id, model_version="test")
    report = SimpleNamespace(id=uuid4(), has_photo=has_photo, model_version="test")
    job = SimpleNamespace(resource_id=item.item_id, cursor=None)
    session = SimpleNamespace(
        get=lambda model, _id: item if model is workers.Catalog else vector,
        scalars=lambda _query: [report],
        scalar=lambda _query: 0.70,
    )
    worker = workers.MatchingWorker(
        SimpleNamespace(context=lambda **_kwargs: nullcontext(session)),
        settings=MatchingSettings(_env_file=None, match_threshold=0.78, match_threshold_text=0.63),
    )
    monkeypatch.setattr(worker, "current", lambda *_args: job)
    monkeypatch.setattr(worker, "finish", lambda *_args: None)
    monkeypatch.setattr(workers, "filters_match", lambda *_args: True)
    saved = []
    monkeypatch.setattr(workers, "save_match", lambda *_args: saved.append(True))
    worker.item_match(job)
    assert bool(saved) is expected


def test_unconfigured_text_cutoff_preserves_the_existing_single_threshold():
    settings = MatchingSettings(_env_file=None, match_threshold=0.42)
    assert settings.automatic_match_threshold(False) == 0.42
    assert settings.automatic_match_threshold(True) == 0.42


def test_candidate_query_scores_in_database_without_returning_dense_vectors():
    from matching_app.service import candidates
    from sqlalchemy import Select
    from sqlalchemy.dialects import postgresql

    statements = []

    def execute(query, *_args):
        if isinstance(query, Select):
            statements.append(str(query.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(all=lambda: [])

    candidates(
        SimpleNamespace(execute=execute),
        [1.0] + [0.0] * 511,
        "test",
        Search(text="backpack"),
        100,
        0.25,
    )
    projection = statements[0].split("\nFROM", 1)[0]
    # One occurrence computes the score; a second would return the raw vector.
    assert projection.count("matching.item_vectors.embedding") == 1
    for column in ["item_version", "tenant_id", "correlation_id", "model_version"]:
        assert "matching.item_vectors." + column in projection
    assert "ORDER BY matching.item_vectors.embedding <=>" in statements[0]


@pytest.mark.parametrize("has_photo,expected", [(False, 0.63), (True, 0.78)])
def test_new_report_matching_passes_its_modality_cutoff_to_candidate_selection(
    monkeypatch, has_photo, expected
):
    import matching_app.workers as workers

    report = SimpleNamespace(
        state="open", embedding=[1.0], model_version="test", has_photo=has_photo
    )
    session = SimpleNamespace(scalar=lambda _query: report)
    job = SimpleNamespace(resource_id=uuid4())
    worker = workers.MatchingWorker(
        SimpleNamespace(context=lambda **_kwargs: nullcontext(session)),
        settings=MatchingSettings(_env_file=None, match_threshold=0.78, match_threshold_text=0.63),
    )
    monkeypatch.setattr(worker, "current", lambda *_args: job)
    monkeypatch.setattr(worker, "finish", lambda *_args: None)
    cutoffs = []
    monkeypatch.setattr(workers, "candidates", lambda *args: cutoffs.append(args[-1]) or [])
    worker.report_match(job)
    assert cutoffs == [expected]


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


def test_loaded_model_readiness_does_not_wait_for_inference():
    encoder = ClipEncoder(None, None)
    encoder.model = object()
    executor = ThreadPoolExecutor(max_workers=1)
    encoder.lock.acquire()
    try:
        executor.submit(encoder.ready).result(timeout=2)
    finally:
        encoder.lock.release()
        executor.shutdown(wait=True)


def test_probes_and_metrics_survive_exhausted_search_thread_pool():
    import anyio.to_thread

    started, released = threading.Event(), threading.Event()
    db = SimpleNamespace(check_ready=lambda *_args: None)
    app = create_app(
        MatchingSettings(_env_file=None, user_client_ids=["owner"]),
        token_verifier=object(),
        database=db,
        internal_database=db,
        encoder=SimpleNamespace(ready=lambda: None),
        item_client=object(),
    )

    @app.get("/occupied-search-worker")
    def occupied():
        started.set()
        released.wait(10)
        return {"done": True}

    async def one_worker():
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
        client.portal.call(one_worker)
        blocking = executor.submit(client.get, "/occupied-search-worker")
        try:
            assert started.wait(2)
            for path in ["/health", "/ready", "/metrics"]:
                assert executor.submit(client.get, path).result(timeout=2).status_code == 200
        finally:
            released.set()
        assert blocking.result(timeout=2).status_code == 200


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
