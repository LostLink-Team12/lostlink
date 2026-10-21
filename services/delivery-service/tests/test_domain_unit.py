from datetime import UTC, datetime
from uuid import uuid4

import pytest
from delivery_app.domain import (
    MAX_DELIVERY_ATTEMPTS,
    Actor,
    Shipment,
    State,
    TransitionError,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def shipment(**overrides):
    values = dict(
        tenant_id=uuid4(), claim_id=uuid4(), item_id=uuid4(), owner_id="owner-1", partner_id="p1"
    )
    values.update(overrides)
    return Shipment(**values)


def move(value, *steps):
    for state, actor in steps:
        value, _ = value.transition(state, actor, NOW)
    return value


def test_happy_path_records_history_and_versions():
    done = move(
        shipment(),
        (State.PICKED_UP, Actor.SYSTEM),
        (State.IN_TRANSIT, Actor.PARTNER),
        (State.OUT_FOR_DELIVERY, Actor.PARTNER),
        (State.DELIVERED, Actor.PARTNER),
    )
    assert done.state is State.DELIVERED and done.is_terminal
    assert done.version == 5 and done.delivery_attempts == 1
    assert [c.to_state for c in done.history] == [
        State.PICKED_UP,
        State.IN_TRANSIT,
        State.OUT_FOR_DELIVERY,
        State.DELIVERED,
    ]


def test_transition_does_not_mutate_original():
    original = shipment()
    updated, change = original.transition(State.PICKED_UP, Actor.SYSTEM, NOW)
    assert original.state is State.AWAITING_PICKUP and original.version == 1
    assert change.from_state is State.AWAITING_PICKUP and change.version == updated.version == 2


def test_partner_cannot_claim_pickup_only_the_venue_scan_can():
    with pytest.raises(TransitionError) as error:
        shipment().transition(State.PICKED_UP, Actor.PARTNER, NOW)
    assert error.value.code == "forbidden_actor"


def test_cannot_skip_states_or_leave_terminal_state():
    with pytest.raises(TransitionError) as skip:
        shipment().transition(State.DELIVERED, Actor.PARTNER, NOW)
    assert skip.value.code == "invalid_transition"
    cancelled = move(shipment(), (State.CANCELLED, Actor.OWNER))
    with pytest.raises(TransitionError) as closed:
        cancelled.transition(State.PICKED_UP, Actor.SYSTEM, NOW)
    assert closed.value.code == "shipment_closed"


def test_owner_cannot_cancel_after_pickup():
    picked = move(shipment(), (State.PICKED_UP, Actor.SYSTEM))
    with pytest.raises(TransitionError):
        picked.transition(State.CANCELLED, Actor.OWNER, NOW)


def test_failed_attempts_are_limited_then_item_returns_to_venue():
    value = move(
        shipment(),
        (State.PICKED_UP, Actor.SYSTEM),
        (State.IN_TRANSIT, Actor.PARTNER),
    )
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        value = move(
            value, (State.OUT_FOR_DELIVERY, Actor.PARTNER), (State.DELIVERY_FAILED, Actor.PARTNER)
        )
    with pytest.raises(TransitionError) as error:
        value.transition(State.OUT_FOR_DELIVERY, Actor.PARTNER, NOW)
    assert error.value.code == "attempts_exhausted"
    returned = move(value, (State.RETURNED_TO_VENUE, Actor.VENUE))
    assert returned.state is State.RETURNED_TO_VENUE


def test_optimistic_concurrency_and_aware_timestamps():
    value = shipment()
    with pytest.raises(TransitionError) as error:
        value.transition(State.PICKED_UP, Actor.SYSTEM, NOW, expected_version=2)
    assert error.value.code == "version_conflict"
    with pytest.raises(ValueError):
        value.transition(State.PICKED_UP, Actor.SYSTEM, datetime(2026, 10, 22))
