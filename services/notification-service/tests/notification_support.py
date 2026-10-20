"""Drive the real Item/Matching pipeline until Matching records match.found."""

from uuid import UUID, uuid4

from item_app.models import Item
from item_app.models import OutboxEvent as ItemOutbox
from lostlink_common.events import DomainEvent
from matching_app.models import OutboxEvent as MatchingOutbox
from sqlalchemy import create_engine, select, text


def headers(actor="owner", **extra):
    return {"Authorization": f"Bearer {actor}", **extra}


def new_report(env, description="red backpack", actor="owner"):
    response = env.matching.post(
        "/v1/lost-reports",
        headers=headers(actor, **{"Idempotency-Key": uuid4().hex}),
        json={"description": description},
    )
    assert response.status_code == 201, response.text
    return response.json(), response.headers["ETag"]


def item_events(env, item_id):
    with env.db["item_worker"].context(tenant_id=env.tenants["a"]) as session:
        rows = list(
            session.scalars(select(ItemOutbox).order_by(ItemOutbox.created_at, ItemOutbox.id))
        )
    events = [DomainEvent.model_validate(row.envelope) for row in rows]
    return [event for event in events if event.data.get("item_id") == str(item_id)]


def matching_events(env, event_type):
    with env.db["matching_worker"].context() as session:
        rows = list(
            session.scalars(
                select(MatchingOutbox).order_by(MatchingOutbox.created_at, MatchingOutbox.id)
            )
        )
    events = [DomainEvent.model_validate(row.envelope) for row in rows]
    return [event for event in events if event.event_type == event_type]


def drain_matching(env):
    for _ in range(200):
        if not env.matching_worker.once():
            return
    raise AssertionError("Matching worker did not drain")


def index_item(env, title="red backpack"):
    response = env.item.post(
        "/v1/items",
        headers=headers("a", **{"Idempotency-Key": uuid4().hex}),
        json={
            "title": title,
            "description": title,
            "category": "bags",
            "found_at": "2026-10-19T09:00:00Z",
            "verification_questions": [{"prompt": "Secret detail?", "reference_answer": "x"}],
        },
    )
    assert response.status_code == 201, response.text
    item_id = UUID(response.json()["id"])
    upload = env.item.post(
        f"/v1/items/{item_id}/uploads",
        headers=headers("a", **{"Idempotency-Key": uuid4().hex, "If-Match": '"1"'}),
        json={"content_type": "image/jpeg", "size": 10},
    )
    assert upload.status_code == 201, upload.text
    confirmed = env.item.post(
        f"/v1/items/{item_id}/uploads/{upload.json()['upload_id']}/confirm",
        headers=headers("a", **{"Idempotency-Key": uuid4().hex}),
    )
    assert confirmed.status_code == 202, confirmed.text
    assert env.item_worker.once("tasks")
    for event in item_events(env, item_id):
        if event.event_type in {"item.registered", "item.catalog_changed"}:
            env.matching_worker.consume(event)
    drain_matching(env)
    for event in matching_events(env, "item.embedded"):
        if event.data["item_id"] == str(item_id):
            env.item_worker.consume(event)
    for event in item_events(env, item_id):
        if event.event_type == "item.catalog_changed":
            env.matching_worker.consume(event)
    drain_matching(env)
    with env.db["item_worker"].context(tenant_id=env.tenants["a"]) as session:
        assert session.get(Item, item_id).state == "available"
    return item_id


def matched(env, *, title="red backpack", actor="owner"):
    """Return (report, report ETag, item ID, match.found event) produced by Matching."""
    report, etag = new_report(env, actor=actor)
    item_id = index_item(env, title)
    found = [
        event
        for event in matching_events(env, "match.found")
        if event.data["report_id"] == report["id"] and event.data["item_id"] == str(item_id)
    ]
    assert len(found) == 1
    return report, etag, item_id, found[0]


def drain(env, limit=20):
    count = 0
    while env.worker.once():
        count += 1
        if count > limit:
            raise AssertionError("Notification worker did not drain")
    return count


def admin_sql(env, statement, **params):
    engine = create_engine(env.migration_url)
    try:
        with engine.begin() as connection:
            result = connection.execute(text(statement), params)
            return result.all() if result.returns_rows else None
    finally:
        engine.dispose()
