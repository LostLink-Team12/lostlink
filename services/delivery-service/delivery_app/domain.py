"""Shipment lifecycle for Seed 2 return delivery.

Delivery owns the transport state after the venue hands an item to a courier. It never
decides who may receive an item: a shipment exists only because the owner created a
delivery authorization for a named partner (implemented with the Partner API in Phase 2),
and pickup is confirmed by the venue scanning the handover QR code, which Claim reports
as ``claim.handed_over`` with ``mode=courier`` (see docs/adr/0009).

The module is pure (no I/O) so the rules can be tested and reused by the API, the event
consumer and the partner webhook handler.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4


class State(StrEnum):
    AWAITING_PICKUP = "awaiting_pickup"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERY_FAILED = "delivery_failed"
    DELIVERED = "delivered"
    RETURNED_TO_VENUE = "returned_to_venue"
    CANCELLED = "cancelled"


class Actor(StrEnum):
    SYSTEM = "system"  # transitions caused by domain events from other services
    PARTNER = "partner"  # the authorized courier, through the Partner API
    OWNER = "owner"
    VENUE = "venue"


TERMINAL = frozenset({State.DELIVERED, State.RETURNED_TO_VENUE, State.CANCELLED})
MAX_DELIVERY_ATTEMPTS = 2

# (from, to) -> actors allowed to cause the transition
TRANSITIONS: dict[tuple[State, State], frozenset[Actor]] = {
    (State.AWAITING_PICKUP, State.PICKED_UP): frozenset({Actor.SYSTEM}),
    (State.AWAITING_PICKUP, State.CANCELLED): frozenset({Actor.OWNER, Actor.SYSTEM}),
    (State.PICKED_UP, State.IN_TRANSIT): frozenset({Actor.PARTNER}),
    (State.IN_TRANSIT, State.OUT_FOR_DELIVERY): frozenset({Actor.PARTNER}),
    (State.OUT_FOR_DELIVERY, State.DELIVERED): frozenset({Actor.PARTNER}),
    (State.OUT_FOR_DELIVERY, State.DELIVERY_FAILED): frozenset({Actor.PARTNER}),
    (State.DELIVERY_FAILED, State.OUT_FOR_DELIVERY): frozenset({Actor.PARTNER}),
    (State.DELIVERY_FAILED, State.RETURNED_TO_VENUE): frozenset({Actor.PARTNER, Actor.VENUE}),
}


class TransitionError(Exception):
    """Raised for a transition the lifecycle does not allow; maps to HTTP 409."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StatusChange:
    shipment_id: UUID
    from_state: State
    to_state: State
    actor: Actor
    version: int
    occurred_at: datetime
    note: str | None = None


@dataclass(frozen=True)
class Shipment:
    tenant_id: UUID
    claim_id: UUID
    item_id: UUID
    owner_id: str
    partner_id: str
    shipment_id: UUID = field(default_factory=uuid4)
    state: State = State.AWAITING_PICKUP
    version: int = 1
    delivery_attempts: int = 0
    history: tuple[StatusChange, ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def transition(
        self,
        to_state: State,
        actor: Actor,
        at: datetime,
        *,
        expected_version: int | None = None,
        note: str | None = None,
    ) -> tuple["Shipment", StatusChange]:
        """Return the new shipment and the change to publish; never mutates self."""
        if at.tzinfo is None:
            raise ValueError("Use timezone-aware timestamps")
        if expected_version is not None and expected_version != self.version:
            raise TransitionError("version_conflict", "Shipment changed; reload and retry")
        if self.is_terminal:
            raise TransitionError("shipment_closed", f"Shipment is already {self.state}")
        allowed = TRANSITIONS.get((self.state, to_state))
        if allowed is None:
            raise TransitionError(
                "invalid_transition", f"Cannot move from {self.state} to {to_state}"
            )
        if actor not in allowed:
            raise TransitionError("forbidden_actor", f"{actor} cannot set {to_state}")

        attempts = self.delivery_attempts
        if to_state is State.OUT_FOR_DELIVERY:
            if attempts >= MAX_DELIVERY_ATTEMPTS:
                raise TransitionError(
                    "attempts_exhausted", "Delivery attempts used up; return the item"
                )
            attempts += 1

        change = StatusChange(
            self.shipment_id, self.state, to_state, actor, self.version + 1, at, note
        )
        updated = replace(
            self,
            state=to_state,
            version=self.version + 1,
            delivery_attempts=attempts,
            history=(*self.history, change),
        )
        return updated, change
