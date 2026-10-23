"""Seed 2 courier return against real PostgreSQL, RLS and the restricted Delivery roles."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from delivery_app.handlers import MissingAuthorization
from delivery_app.models import DATABASE_REVISION, DATABASE_SCHEMA, OutboxEvent, ShipmentRecord
from lostlink_common.events import DomainEvent
from sqlalchemy import select, text

pytestmark = pytest.mark.integration

ADDRESS = {
    "recipient_name": "Alex Owner",
    "address_line": "1 Example Road #01-01",
    "postal_code": "123456",
    "phone": "+65 8000 0000",
}


def headers(actor, *, key=None, version=None):
    value = {"Authorization": f"Bearer {actor}"}
    if key:
        value["Idempotency-Key"] = key
    if version is not None:
        value["If-Match"] = f'"{version}"'
    return value


def claim_event(env, event_type, claim_id, item_id, owner="owner-1", **extra):
    return DomainEvent(
        event_type=event_type,
        source="claim-service",
        tenant_id=env.tenant,
        data={"claim_id": str(claim_id), "item_id": str(item_id), "owner_id": owner, **extra},
    )


def approved_claim(env, owner="owner-1"):
    claim_id, item_id = uuid4(), uuid4()
    code = env.worker.consume(claim_event(env, "claim.approved", claim_id, item_id, owner))
    assert code == "approved"
    return claim_id, item_id


def handed_over(env, claim_id, item_id, mode="courier"):
    return claim_event(
        env,
        "claim.handed_over",
        claim_id,
        item_id,
        mode=mode,
        handed_over_at=datetime.now(UTC).isoformat(),
    )


def authorize(env, claim_id, partner="courier-a", actor="owner", key=None):
    return env.client.post(
        "/v1/delivery-authorizations",
        headers=headers(actor, key=key or f"auth-{uuid4().hex[:12]}"),
        json={"claim_id": str(claim_id), "partner_id": partner, "delivery_address": ADDRESS},
    )


def partner_update(env, shipment_id, state, version, partner="courier-a"):
    return env.client.post(
        f"/v1/partner/shipments/{shipment_id}/status",
        headers=headers(partner, key=f"upd-{uuid4().hex[:12]}", version=version),
        json={"state": state},
    )


def outbox(env):
    with env.db["worker"].system_session() as session:
        rows = list(session.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at)))
    return [DomainEvent.model_validate(row.envelope) for row in rows]


def test_schema_roles_and_readiness(delivery_env):
    env = delivery_env
    for db in env.db.values():
        db.check_ready(DATABASE_SCHEMA, DATABASE_REVISION)
    assert env.client.get("/ready").status_code == 200
    with env.db["api"].engine.connect() as connection:
        assert not connection.execute(
            text("SELECT has_schema_privilege(current_user, 'delivery', 'CREATE')")
        ).scalar_one()
        assert not connection.execute(
            text("SELECT has_table_privilege(current_user, 'delivery.inbox_receipts', 'SELECT')")
        ).scalar_one()
        assert not connection.execute(
            text(
                "SELECT has_column_privilege(current_user, 'delivery.shipments', "
                "'partner_id', 'UPDATE')"
            )
        ).scalar_one()


def test_courier_return_main_flow(delivery_env):
    env = delivery_env
    claim_id, item_id = approved_claim(env)

    key = "authorize-main-flow"
    created = authorize(env, claim_id, key=key)
    assert created.status_code == 201, created.text
    shipment = created.json()
    assert shipment["state"] == "awaiting_pickup" and created.headers["ETag"] == '"1"'
    assert "delivery_address" not in shipment  # idempotency snapshots are plaintext
    owned = env.client.get(f"/v1/shipments/{shipment['id']}", headers=headers("owner")).json()
    assert owned["delivery_address"] == ADDRESS
    assert [h["to_state"] for h in shipment["history"]] == ["awaiting_pickup"]
    replay = authorize(env, claim_id, key=key)
    assert replay.status_code == 201 and replay.json()["id"] == shipment["id"]
    assert authorize(env, claim_id).json()["error"]["code"] == "shipment_exists"

    with env.db["worker"].system_session() as session:
        row = session.get(ShipmentRecord, UUID(shipment["id"]))
        assert b"Example Road" not in row.address_ciphertext

    listed = env.client.get("/v1/partner/shipments", headers=headers("courier-a")).json()
    assert [s["id"] for s in listed["items"]] == [shipment["id"]]
    assert listed["items"][0].get("delivery_address") is None
    other = env.client.get("/v1/partner/shipments", headers=headers("courier-b")).json()
    assert other["items"] == []
    hidden = env.client.get(f"/v1/partner/shipments/{shipment['id']}", headers=headers("courier-b"))
    assert hidden.status_code == 404
    detail = env.client.get(
        f"/v1/partner/shipments/{shipment['id']}", headers=headers("courier-a")
    ).json()
    assert detail["delivery_address"] == ADDRESS

    # A partner can never claim pickup itself; only the venue's QR scan does.
    early = partner_update(env, shipment["id"], "in_transit", 1)
    assert early.status_code == 409 and early.json()["error"]["code"] == "invalid_transition"

    event = handed_over(env, claim_id, item_id)
    assert env.worker.consume(event) == "picked_up"
    assert env.worker.consume(event) == "duplicate"
    assert env.worker.consume(handed_over(env, claim_id, item_id)) == "duplicate_pickup"

    stale = partner_update(env, shipment["id"], "in_transit", 1)
    assert stale.status_code == 412
    version = 2
    for state in ["in_transit", "out_for_delivery", "delivered"]:
        response = partner_update(env, shipment["id"], state, version)
        assert response.status_code == 200, response.text
        version += 1
        assert response.headers["ETag"] == f'"{version}"'
    closed = partner_update(env, shipment["id"], "delivery_failed", version)
    assert closed.status_code == 409 and closed.json()["error"]["code"] == "shipment_closed"

    final = env.client.get(f"/v1/shipments/{shipment['id']}", headers=headers("owner")).json()
    assert final["state"] == "delivered" and final["terminal"]
    assert final["delivery_attempts"] == 1
    assert [(h["from_state"], h["to_state"], h["actor"]) for h in final["history"]] == [
        (None, "awaiting_pickup", "owner"),
        ("awaiting_pickup", "picked_up", "system"),
        ("picked_up", "in_transit", "partner"),
        ("in_transit", "out_for_delivery", "partner"),
        ("out_for_delivery", "delivered", "partner"),
    ]
    after = env.client.get(
        f"/v1/partner/shipments/{shipment['id']}", headers=headers("courier-a")
    ).json()
    assert after.get("delivery_address") is None

    events = outbox(env)
    assert [e.data["to_state"] for e in events] == [
        "awaiting_pickup",
        "picked_up",
        "in_transit",
        "out_for_delivery",
        "delivered",
    ]
    assert all(e.event_type == "delivery.status_changed" for e in events)
    assert "Example Road" not in str([e.model_dump(mode="json") for e in events])

    env.publisher.failures = 1
    while env.publisher_worker.publish_once():
        pass
    assert [e.event_id for e in env.publisher.events] == [e.event_id for e in events[1:]]
    with env.db["worker"].system_session() as session:
        states = session.scalars(select(OutboxEvent.state)).all()
    assert sorted(states) == ["pending"] + ["sent"] * 4


def test_failed_attempts_return_the_item_to_the_venue(delivery_env):
    env = delivery_env
    claim_id, item_id = approved_claim(env)
    shipment = authorize(env, claim_id).json()
    env.worker.consume(handed_over(env, claim_id, item_id))
    version = 2
    for state in [
        "in_transit",
        "out_for_delivery",
        "delivery_failed",
        "out_for_delivery",
        "delivery_failed",
    ]:
        response = partner_update(env, shipment["id"], state, version)
        assert response.status_code == 200, response.text
        version += 1
    exhausted = partner_update(env, shipment["id"], "out_for_delivery", version)
    assert exhausted.json()["error"]["code"] == "attempts_exhausted"
    returned = partner_update(env, shipment["id"], "returned_to_venue", version)
    assert returned.status_code == 200 and returned.json()["delivery_attempts"] == 2


def test_courier_pickup_without_authorization_is_an_alarm(delivery_env):
    env = delivery_env
    claim_id, item_id = approved_claim(env)
    event = handed_over(env, claim_id, item_id)
    for _ in range(2):  # rolled back, so redelivery fails the same way until the DLQ
        with pytest.raises(MissingAuthorization):
            env.worker.consume(event)
    counter_claim, counter_item = approved_claim(env)
    counter = handed_over(env, counter_claim, counter_item, mode="counter")
    assert env.worker.consume(counter) == "counter"
    # The claim is closed, so the owner can no longer authorize a courier for it.
    late = authorize(env, counter_claim)
    assert late.status_code == 409 and late.json()["error"]["code"] == "claim_not_approved"


def test_cancellation_and_owner_isolation(delivery_env):
    env = delivery_env
    claim_id, item_id = approved_claim(env)
    assert authorize(env, claim_id, actor="other").status_code == 404
    unknown = authorize(env, claim_id, partner="courier-z")
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "unknown_partner"
    shipment = authorize(env, claim_id).json()
    for actor in ["other", "courier-a"]:
        response = env.client.get(f"/v1/shipments/{shipment['id']}", headers=headers(actor))
        assert response.status_code in {403, 404}, actor
    assert env.client.get("/v1/shipments", headers=headers("other")).json()["items"] == []

    cancelled = claim_event(
        env, "claim.cancelled", claim_id, item_id, reason_code="found_elsewhere"
    )
    assert env.worker.consume(cancelled) == "shipment_cancelled"
    final = env.client.get(f"/v1/shipments/{shipment['id']}", headers=headers("owner")).json()
    assert final["state"] == "cancelled"
    assert final["history"][-1]["note"] == "found_elsewhere"

    second_claim, _ = approved_claim(env)
    second = authorize(env, second_claim).json()
    path = f"/v1/shipments/{second['id']}/cancel"
    missing = env.client.post(path, headers=headers("owner", key="cancel-no-version"))
    assert missing.status_code == 428
    done = env.client.post(path, headers=headers("owner", key="cancel-second-1", version=1))
    assert done.status_code == 200 and done.json()["state"] == "cancelled"


def test_counter_collection_revokes_pending_shipment_and_partner_address(delivery_env):
    env = delivery_env
    claim_id, item_id = approved_claim(env)
    shipment = authorize(env, claim_id).json()
    detail_path = f"/v1/partner/shipments/{shipment['id']}"
    before = env.client.get(detail_path, headers=headers("courier-a"))
    assert before.json()["delivery_address"] == ADDRESS

    event = handed_over(env, claim_id, item_id, mode="counter")
    assert env.worker.consume(event) == "shipment_cancelled"
    assert env.worker.consume(event) == "duplicate"
    assert env.worker.consume(handed_over(env, claim_id, item_id, mode="counter")) == "counter"

    after = env.client.get(detail_path, headers=headers("courier-a"))
    assert after.status_code == 200
    assert after.json()["state"] == "cancelled" and after.json()["terminal"]
    assert after.headers["ETag"] == '"2"'
    assert after.json().get("delivery_address") is None
    assert "Example Road" not in after.text
    assert [entry["to_state"] for entry in after.json()["history"]] == [
        "awaiting_pickup",
        "cancelled",
    ]
    assert after.json()["history"][-1]["note"] == "collected_at_counter"
    active = env.client.get("/v1/partner/shipments", headers=headers("courier-a"))
    assert active.json()["items"] == []
    assert partner_update(env, shipment["id"], "in_transit", 2).status_code == 409
    assert authorize(env, claim_id).json()["error"]["code"] == "claim_not_approved"
    events = outbox(env)
    assert [event.data["to_state"] for event in events] == ["awaiting_pickup", "cancelled"]
    assert "Example Road" not in str([event.model_dump(mode="json") for event in events])


def test_authorization_waits_for_claim_closure_without_projection_write_grants(delivery_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from delivery_app.models import ClaimApproval
    from sqlalchemy import event

    env = delivery_env
    claim_id, _ = approved_claim(env)
    reached_lock = Event()

    def observe_lock(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            reached_lock.set()

    event.listen(env.db["api"].engine, "before_cursor_execute", observe_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with env.worker.session() as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": int.from_bytes(claim_id.bytes[:8], signed=True)},
                )
                approval = session.get(ClaimApproval, claim_id, with_for_update=True)
                approval.state = "handed_over"
                future = executor.submit(authorize, env, claim_id)
                assert reached_lock.wait(5)
                assert not future.done()
            # The blocked API reads the committed terminal projection after acquiring the lock.
            response = future.result(timeout=5)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "claim_not_approved"
    finally:
        event.remove(env.db["api"].engine, "before_cursor_execute", observe_lock)
    assert env.client.get("/v1/shipments", headers=headers("owner")).json()["items"] == []
