from datetime import UTC, datetime
from uuid import uuid4

import pytest
from delivery_app.domain import Actor, Shipment, State
from delivery_app.handlers import MissingAuthorization, on_claim_cancelled, on_claim_handed_over

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
TENANT = uuid4()


def shipment():
    return Shipment(
        tenant_id=TENANT, claim_id=uuid4(), item_id=uuid4(), owner_id="owner-1", partner_id="p1"
    )


def handed_over(value, mode="courier"):
    return {
        "claim_id": str(value.claim_id),
        "item_id": str(value.item_id),
        "owner_id": value.owner_id,
        "mode": mode,
        "handed_over_at": NOW.isoformat(),
    }


def test_courier_handover_marks_pickup_once():
    value = shipment()
    updated, change = on_claim_handed_over(handed_over(value), TENANT, value)
    assert updated.state is State.PICKED_UP and change.occurred_at == NOW
    assert on_claim_handed_over(handed_over(updated), TENANT, updated) is None


def test_counter_handover_is_ignored():
    value = shipment()
    assert on_claim_handed_over(handed_over(value, "counter"), TENANT, value) is None


def test_courier_handover_without_authorization_is_an_alarm():
    value = shipment()
    with pytest.raises(MissingAuthorization):
        on_claim_handed_over(handed_over(value), TENANT, None)
    with pytest.raises(MissingAuthorization):
        on_claim_handed_over(handed_over(value), uuid4(), value)


def test_payload_is_validated_with_the_shared_schema():
    value = shipment()
    payload = handed_over(value) | {"unexpected": True}
    with pytest.raises(ValueError):
        on_claim_handed_over(payload, TENANT, value)


def test_cancelled_claim_cancels_only_before_pickup():
    value = shipment()
    data = {
        "claim_id": str(value.claim_id),
        "item_id": str(value.item_id),
        "owner_id": value.owner_id,
        "reason_code": "found_elsewhere",
    }
    updated, change = on_claim_cancelled(data, value, NOW)
    assert updated.state is State.CANCELLED and change.note == "found_elsewhere"
    picked, _ = value.transition(State.PICKED_UP, Actor.SYSTEM, NOW)
    assert on_claim_cancelled(data, picked, NOW) is None
