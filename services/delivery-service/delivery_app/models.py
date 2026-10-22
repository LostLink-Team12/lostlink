"""Delivery-owned tables (schema ``delivery``). The address is stored only encrypted."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from delivery_app.domain import State

DATABASE_SCHEMA = "delivery"
DATABASE_REVISION = "0001_delivery"
SHIPMENT_STATES = tuple(state.value for state in State)
APPROVAL_STATES = ("approved", "handed_over", "cancelled")


def in_list(values):
    return ", ".join(f"'{value}'" for value in values)


class Base(DeclarativeBase):
    metadata = MetaData(schema=DATABASE_SCHEMA)


class ClaimApproval(Base):
    """Projection of claim.* events: which owner may authorize delivery for which claim."""

    __tablename__ = "claim_approvals"
    __table_args__ = (
        CheckConstraint(f"state IN ({in_list(APPROVAL_STATES)})", name="ck_claim_approval_state"),
        Index("ix_claim_approvals_owner", "owner_sub"),
    )
    claim_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    item_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), default="approved")
    source_event_id: Mapped[UUID]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShipmentRecord(Base):
    __tablename__ = "shipments"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({in_list(SHIPMENT_STATES)}) AND version > 0 "
            "AND delivery_attempts BETWEEN 0 AND 2",
            name="ck_shipment_state",
        ),
        UniqueConstraint("claim_id", name="uq_shipment_claim"),
        Index("ix_shipments_owner", "owner_sub", "created_at", "id"),
        Index("ix_shipments_partner", "partner_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID]
    claim_id: Mapped[UUID]
    item_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    partner_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24), default=State.AWAITING_PICKUP.value)
    version: Mapped[int] = mapped_column(default=1)
    delivery_attempts: Mapped[int] = mapped_column(default=0)
    address_key_id: Mapped[str] = mapped_column(String(64))
    address_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShipmentEvent(Base):
    """Append-only status history; owner and partner columns drive row-level security."""

    __tablename__ = "shipment_events"
    __table_args__ = (UniqueConstraint("shipment_id", "version", name="uq_shipment_event"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    shipment_id: Mapped[UUID] = mapped_column(ForeignKey("delivery.shipments.id"))
    tenant_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    partner_id: Mapped[str] = mapped_column(String(128))
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(16))
    version: Mapped[int]
    note: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("state IN ('pending','leased','sent','failed')", name="ck_outbox_state"),
        Index("ix_outbox_poll", "state", "available_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID]
    envelope: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InboxReceipt(Base):
    __tablename__ = "inbox_receipts"
    consumer_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    result_code: Mapped[str] = mapped_column(String(32))
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_sub: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSONB)
    response_status: Mapped[int]
    etag: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
