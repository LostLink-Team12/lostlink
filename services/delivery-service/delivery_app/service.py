"""Persistence for the pure lifecycle in domain.py: rows, status history and the outbox.

Every state change writes the new row state, one ``shipment_events`` row and one
``delivery.status_changed`` outbox event in the same transaction (ADR-0008), so the
published history can never disagree with the stored one.
"""

import json
from datetime import datetime
from uuid import UUID

from lostlink_common.crypto import EncryptedValue, EncryptionContext
from lostlink_common.events import DomainEvent
from sqlalchemy import select

from delivery_app.domain import TERMINAL, Shipment, State, StatusChange
from delivery_app.models import OutboxEvent, ShipmentEvent, ShipmentRecord

SERVICE = "delivery-service"


def iso(value):
    return value.isoformat() if value else None


def etag(row):
    return f'"{row.version}"'


def address_context(shipment_id: UUID) -> EncryptionContext:
    return EncryptionContext(SERVICE, "shipments", "address_ciphertext", shipment_id)


def seal_address(cipher, shipment_id: UUID, address: dict) -> EncryptedValue:
    payload = json.dumps(address, sort_keys=True, separators=(",", ":")).encode()
    return cipher.encrypt(payload, address_context(shipment_id))


def open_address(cipher, row) -> dict:
    value = EncryptedValue(row.address_key_id, row.address_ciphertext)
    return json.loads(cipher.decrypt(value, address_context(row.id)))


def to_domain(row: ShipmentRecord) -> Shipment:
    return Shipment(
        tenant_id=row.tenant_id,
        claim_id=row.claim_id,
        item_id=row.item_id,
        owner_id=row.owner_sub,
        partner_id=row.partner_id,
        shipment_id=row.id,
        state=State(row.state),
        version=row.version,
        delivery_attempts=row.delivery_attempts,
    )


def record(session, row, *, from_state, to_state, actor, version, at: datetime, note=None):
    """Append history and the outbox event for a change already applied to ``row``."""
    session.add(
        ShipmentEvent(
            shipment_id=row.id,
            tenant_id=row.tenant_id,
            owner_sub=row.owner_sub,
            partner_id=row.partner_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            version=version,
            note=note,
            occurred_at=at,
        )
    )
    event = DomainEvent(
        event_type="delivery.status_changed",
        source=SERVICE,
        tenant_id=row.tenant_id,
        occurred_at=at,
        data={
            "shipment_id": str(row.id),
            "claim_id": str(row.claim_id),
            "item_id": str(row.item_id),
            "owner_id": row.owner_sub,
            "partner_id": row.partner_id,
            "from_state": from_state,
            "to_state": to_state,
            "actor": actor,
            "version": version,
            "occurred_at": at.isoformat(),
        },
    )
    session.add(
        OutboxEvent(
            id=event.event_id, tenant_id=row.tenant_id, envelope=event.model_dump(mode="json")
        )
    )


def apply(session, row: ShipmentRecord, updated: Shipment, change: StatusChange) -> None:
    row.state = updated.state.value
    row.version = updated.version
    row.delivery_attempts = updated.delivery_attempts
    row.updated_at = change.occurred_at
    record(
        session,
        row,
        from_state=change.from_state.value,
        to_state=change.to_state.value,
        actor=change.actor.value,
        version=change.version,
        at=change.occurred_at,
        note=change.note,
    )
    session.flush()


def history(session, shipment_id: UUID) -> list[dict]:
    rows = session.scalars(
        select(ShipmentEvent)
        .where(ShipmentEvent.shipment_id == shipment_id)
        .order_by(ShipmentEvent.version)
    )
    return [
        {
            "from_state": event.from_state,
            "to_state": event.to_state,
            "actor": event.actor,
            "version": event.version,
            "note": event.note,
            "occurred_at": iso(event.occurred_at),
        }
        for event in rows
    ]


def view(row: ShipmentRecord, *, events=None, address=None) -> dict:
    value = {
        "id": str(row.id),
        "claim_id": str(row.claim_id),
        "item_id": str(row.item_id),
        "partner_id": row.partner_id,
        "state": row.state,
        "version": row.version,
        "delivery_attempts": row.delivery_attempts,
        "terminal": State(row.state) in TERMINAL,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }
    if events is not None:
        value["history"] = events
    if address is not None:
        value["delivery_address"] = address
    return value
