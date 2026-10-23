"""Helpers for driving real Item, Claim and worker flows in integration tests."""

from uuid import UUID, uuid4

from claim_app.models import OutboxEvent
from item_app.models import Item, Reservation
from lostlink_common.events import DomainEvent
from sqlalchemy import select


def headers(actor="owner", *, key=None, version=None):
    value = {"Authorization": f"Bearer {actor}", "Idempotency-Key": key or uuid4().hex}
    if version is not None:
        value["If-Match"] = version if isinstance(version, str) else f'"{version}"'
    return value


def available_item(env, venue="a", *, title="Black wallet"):
    response = env.item.post(
        "/v1/items",
        headers=headers(venue),
        json={
            "title": title,
            "description": f"{title} left at the counter",
            "category": "wallet",
            "found_at": "2026-10-19T09:00:00Z",
            "verification_questions": [
                {"prompt": "What card is inside?", "reference_answer": "PRIVATE-LIBRARY"},
                {"prompt": "What colour is the zip?", "reference_answer": "PRIVATE-RED"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    item = response.json()
    upload = env.item.post(
        f"/v1/items/{item['id']}/uploads",
        headers=headers(venue, version=item["version"]),
        json={"content_type": "image/png", "size": 3},
    )
    assert upload.status_code == 201, upload.text
    confirmed = env.item.post(
        f"/v1/items/{item['id']}/uploads/{upload.json()['upload_id']}/confirm",
        headers=headers(venue),
    )
    assert confirmed.status_code == 202, confirmed.text
    assert env.item_worker.once("tasks")
    context = env.item.get(
        f"/internal/v1/items/{item['id']}/index-context", headers=headers("index")
    ).json()
    event = DomainEvent(
        event_type="item.embedded",
        source="embedding-worker",
        tenant_id=env.tenants[venue],
        correlation_id=UUID(context["processing_correlation_id"]),
        data={
            "item_id": item["id"],
            "item_version": context["item_version"],
            "model_version": "test",
            "dimensions": 512,
        },
    )
    assert env.item_worker.consume(event) == "applied"
    return UUID(item["id"])


def questions(env, item_id, actor="owner"):
    response = env.client.get(
        "/v1/claim-questions", params={"item_id": str(item_id)}, headers=headers(actor)
    )
    assert response.status_code == 200, response.text
    return response.json()


def answers_for(env, item_id, actor="owner"):
    return [
        {"question_id": q["id"], "version": q["version"], "answer": f"answer {n}"}
        for n, q in enumerate(questions(env, item_id, actor)["questions"])
    ]


def submit(env, item_id, actor="owner", **extra):
    body = {
        "item_id": str(item_id),
        "answers": answers_for(env, item_id, actor),
        "contact": {"name": "Alex Owner", "phone": "+65 8000 0000"},
        **extra,
    }
    response = env.client.post("/v1/claims", headers=headers(actor), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def drain(env, limit=20):
    count = 0
    while env.worker.once("tasks"):
        count += 1
        if count > limit:
            raise AssertionError("Claim worker did not drain")
    return count


def approve(env, claim, venue="a", *, run=True):
    response = env.client.post(
        f"/v1/venue/claims/{claim['id']}/approve", headers=headers(venue, version=claim["version"])
    )
    assert response.status_code == 202, response.text
    if run:
        drain(env)
    return response.json()


def owner_claim(env, claim_id, actor="owner"):
    response = env.client.get(f"/v1/claims/{claim_id}", headers=headers(actor))
    assert response.status_code == 200, response.text
    return response.json()


def item_state(env, item_id, venue="a"):
    with env.db["item_worker"].context(tenant_id=env.tenants[venue]) as session:
        item = session.get(Item, item_id)
        reservations = list(
            session.scalars(select(Reservation).where(Reservation.item_id == item_id))
        )
        return item.state, [(r.claim_id, r.state, r.handover_id) for r in reservations]


def claim_events(env, claim_id=None):
    with env.db["claim_worker"].system_session() as session:
        rows = list(session.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at)))
    events = [DomainEvent.model_validate(row.envelope) for row in rows]
    return [e for e in events if claim_id is None or e.data["claim_id"] == str(claim_id)]


def issue_token(env, claim_id, actor="owner", key=None):
    response = env.client.post(
        f"/v1/claims/{claim_id}/handover-token", headers=headers(actor, key=key)
    )
    assert response.status_code == 201, response.text
    return response.json()


def scan(env, claim_id, token, venue="a", *, version, key=None, mode=None):
    body = {"token": token} if mode is None else {"token": token, "mode": mode}
    return env.client.post(
        f"/v1/venue/claims/{claim_id}/handover",
        headers=headers(venue, version=version, key=key),
        json=body,
    )
