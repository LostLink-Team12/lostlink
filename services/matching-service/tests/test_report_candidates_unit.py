"""Saved-query browsing retains live Item checks and paginates eligible candidates."""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from lostlink_common.auth import Principal
from matching_app import routes
from matching_app.config import MatchingSettings
from matching_app.main import create_app


@pytest.fixture
def browsing(monkeypatch):
    tenant, correlation = uuid4(), uuid4()
    contexts, rows, called, transaction = {}, [], [], []
    row = SimpleNamespace(
        state="open",
        embedding=[1.0],
        model_version="saved-model",
        category=None,
        venue_id=None,
        found_from=None,
        found_until=None,
    )

    @contextmanager
    def context(**_):
        transaction.append(True)
        try:
            yield object()
        finally:
            transaction.pop()

    def candidates(*args):
        called.append(args)
        return rows

    def current(item_id):
        assert not transaction, "Item calls must remain outside database transactions"
        return contexts[item_id]

    monkeypatch.setattr(routes, "report", lambda *_: row)
    monkeypatch.setattr(routes, "candidates", candidates)
    verifier = SimpleNamespace(
        verify=lambda _: Principal(subject="owner", client_id="web", roles={"owner"})
    )
    db = SimpleNamespace(context=context, check_ready=lambda *_: None)
    encoder = SimpleNamespace(
        ready=lambda: None,
        encode=lambda *_: pytest.fail("Saved queries must reuse their original vector"),
    )
    app = create_app(
        MatchingSettings(
            _env_file=None, user_client_ids=["web"], match_threshold=0.99, match_threshold_text=0.99
        ),
        token_verifier=verifier,
        database=db,
        internal_database=db,
        encoder=encoder,
        item_client=SimpleNamespace(context=current),
    )
    for number in range(25):
        item_id = uuid4()
        item = SimpleNamespace(item_id=item_id, tenant_id=tenant)
        embedding = SimpleNamespace(item_version=1, correlation_id=correlation)
        rows.append((item, embedding, 0.4))
        contexts[item_id] = SimpleNamespace(
            item_id=item_id,
            tenant_id=tenant,
            item_version=1,
            processing_correlation_id=correlation,
            state="available",
            title=f"Item {number}",
            description="Bottle",
            category="Bottle",
            found_at=datetime(2026, 10, 24, tzinfo=UTC),
            public_location_hint=None,
            photo_url="https://photos.invalid/item",
        )
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            row=row,
            rows=rows,
            contexts=contexts,
            called=called,
            path=f"/v1/lost-reports/{uuid4()}/candidates",
            headers={"Authorization": "Bearer owner"},
        )


def test_browse_includes_lower_confidence_items_and_pages_after_live_filtering(browsing):
    env = browsing
    for item, *_ in env.rows[:2]:
        env.contexts[item.item_id].state = "reserved"
    first = env.client.get(env.path, headers=env.headers)
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    assert len(first.json()["items"]) == 20 and first.json()["has_more"]
    second = env.client.get(env.path + "?offset=20", headers=env.headers)
    assert len(second.json()["items"]) == 3 and not second.json()["has_more"]
    first_ids = {row["item_id"] for row in first.json()["items"]}
    second_ids = {row["item_id"] for row in second.json()["items"]}
    assert not first_ids & second_ids and len(first_ids | second_ids) == 23
    assert all(call[-1] == 0.0 and call[2] == "saved-model" for call in env.called)
    assert env.client.get(env.path, headers=env.headers).json()["items"][0]["score"] == 0.4


def test_closed_report_never_queries_candidates_or_live_items(browsing):
    env = browsing
    env.row.state = "closed"
    response = env.client.get(env.path, headers=env.headers)
    assert response.status_code == 200
    assert response.json()["items"] == [] and not response.json()["has_more"]
    assert env.called == []


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001"])
def test_browse_bounds_are_enforced(browsing, query):
    env = browsing
    assert env.client.get(env.path + "?" + query, headers=env.headers).status_code == 422
