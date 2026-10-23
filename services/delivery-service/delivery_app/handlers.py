"""Reactions to other services' events. Pure functions; the consumer adds I/O."""

from datetime import datetime
from uuid import UUID

from lostlink_common.event_schemas import ClaimCancelled, ClaimHandedOver

from delivery_app.domain import Actor, Shipment, State, StatusChange


class MissingAuthorization(Exception):
    """A courier pickup arrived for a claim with no delivery authorization.

    The consumer must not drop the message: it is an integrity alarm (Claim and
    Delivery disagree), so it goes to the DLQ and pages the on-call member.
    """


def on_claim_handed_over(
    data: dict, tenant_id: UUID, shipment: Shipment | None
) -> tuple[Shipment, StatusChange] | None:
    """Confirm courier pickup or revoke an unused shipment after counter collection."""
    event = ClaimHandedOver.model_validate(data)
    if shipment is None:
        if event.mode == "counter":
            return None
        raise MissingAuthorization(f"No delivery authorization for claim {event.claim_id}")
    if (
        shipment.claim_id != event.claim_id
        or shipment.tenant_id != tenant_id
        or shipment.item_id != event.item_id
        or shipment.owner_id != event.owner_id
    ):
        raise MissingAuthorization("Handover does not match the authorized shipment")
    if event.mode == "counter":
        if shipment.state is State.CANCELLED:
            return None
        # The owner collected the item; the courier no longer needs the address.
        # Transport states deliberately reject this conflicting event for DLQ investigation.
        return shipment.transition(
            State.CANCELLED, Actor.SYSTEM, event.handed_over_at, note="collected_at_counter"
        )
    if shipment.state is State.PICKED_UP:
        return None  # duplicate delivery of an at-least-once event
    return shipment.transition(State.PICKED_UP, Actor.SYSTEM, event.handed_over_at)


def on_claim_cancelled(
    data: dict, shipment: Shipment | None, at: datetime
) -> tuple[Shipment, StatusChange] | None:
    """A cancelled claim cancels a shipment that has not been picked up yet."""
    event = ClaimCancelled.model_validate(data)
    if shipment is None or shipment.claim_id != event.claim_id:
        return None
    if shipment.state is not State.AWAITING_PICKUP:
        return None
    return shipment.transition(State.CANCELLED, Actor.SYSTEM, at, note=event.reason_code)
