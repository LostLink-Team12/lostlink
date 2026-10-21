"""Helpers for exercising real Item and Matching contracts."""

import base64
from uuid import UUID, uuid4

from item_app.models import Item
from item_app.models import OutboxEvent as ItemOutbox
from lostlink_common.events import DomainEvent
from sqlalchemy import select


def headers(actor="owner", **extra):
    return {"Authorization": f"Bearer {actor}", **extra}


def new_report(env, description="red backpack", **extra):
    response = env.client.post(
        "/v1/lost-reports",
        headers=headers(**{"Idempotency-Key": uuid4().hex}),
        json={"description": description, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def item_events(env, item_id):
    with env.db["item_worker"].context(tenant_id=env.tenants["a"]) as session:
        # Operational rows use the worker policy; tenant context is irrelevant here.
        rows = list(
            session.scalars(select(ItemOutbox).order_by(ItemOutbox.created_at, ItemOutbox.id))
        )
    return [
        DomainEvent.model_validate(row.envelope)
        for row in rows
        if row.envelope["data"].get("item_id") == str(item_id)
    ]


def create_item(env, actor="a", description="red backpack"):
    response = env.item.post(
        "/v1/items",
        headers=headers(actor, **{"Idempotency-Key": uuid4().hex}),
        json={
            "title": description,
            "description": description,
            "category": "bags",
            "found_at": "2026-10-19T09:00:00Z",
            "public_location_hint": "Reception",
            "verification_questions": [{"prompt": "Secret detail?", "reference_answer": "private"}],
        },
    )
    assert response.status_code == 201, response.text
    item_id = UUID(response.json()["id"])
    upload = env.item.post(
        f"/v1/items/{item_id}/uploads",
        headers=headers(
            actor, **{"Idempotency-Key": uuid4().hex, "If-Match": response.headers["ETag"]}
        ),
        json={"content_type": "image/jpeg", "size": 10},
    )
    assert upload.status_code == 201, upload.text
    confirmed = env.item.post(
        f"/v1/items/{item_id}/uploads/{upload.json()['upload_id']}/confirm",
        headers=headers(actor, **{"Idempotency-Key": uuid4().hex}),
        json={},
    )
    assert confirmed.status_code == 202, confirmed.text
    assert env.item_worker.once("tasks")
    return item_id


def drain(env):
    for _ in range(200):
        if not env.worker.once():
            return
    raise AssertionError("Worker did not drain")


def index_item(env, actor="a", description="red backpack"):
    item_id = create_item(env, actor, description)
    for event in item_events(env, item_id):
        if event.event_type in {"item.registered", "item.catalog_changed"}:
            env.worker.consume(event)
    drain(env)
    from matching_app.models import OutboxEvent

    with env.db["matching_worker"].context() as session:
        results = list(session.scalars(select(OutboxEvent)))
    for row in results:
        event = DomainEvent.model_validate(row.envelope)
        if event.event_type in {"item.embedded", "item.embedding_failed"}:
            env.item_worker.consume(event)
    for event in item_events(env, item_id):
        if event.event_type == "item.catalog_changed":
            env.worker.consume(event)
    drain(env)
    with env.db["item_worker"].context(tenant_id=env.tenants[actor]) as session:
        assert session.get(Item, item_id).state == "available"
    return item_id


PHOTO = base64.b64encode(b"valid test photo").decode()
