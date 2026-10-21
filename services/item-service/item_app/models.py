"""Private service-owned domain tables and pointer-only operational tasks."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_SCHEMA = "item"
DATABASE_REVISION = "0001_item"


class Base(DeclarativeBase):
    metadata = MetaData(schema=DATABASE_SCHEMA)


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Item(Record, Base):
    __tablename__ = "items"
    __table_args__ = (
        Index("ix_items_tenant_created", "tenant_id", "created_at", "id"),
        CheckConstraint(
            "state IN ('awaiting_upload','processing','processing_failed',"
            "'available','reserved','handed_over','withdrawn') AND "
            "item_version > 0 AND version > 0",
            name="ck_item_state",
        ),
    )
    tenant_id: Mapped[UUID]
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000))
    category: Mapped[str] = mapped_column(String(100))
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    public_location_hint: Mapped[str | None] = mapped_column(String(300))
    state: Mapped[str] = mapped_column(String(32), default="awaiting_upload")
    item_version: Mapped[int] = mapped_column(default=1)
    version: Mapped[int] = mapped_column(default=1)
    processing_correlation_id: Mapped[UUID | None]
    active_upload_id: Mapped[UUID | None]
    photo_id: Mapped[UUID | None]
    reserved_claim_id: Mapped[UUID | None]
    created_by: Mapped[str] = mapped_column(String(128))


class Question(Record, Base):
    __tablename__ = "verification_questions"
    tenant_id: Mapped[UUID]
    item_id: Mapped[UUID] = mapped_column(ForeignKey("item.items.id"), index=True)
    prompt: Mapped[str] = mapped_column(String(300))
    reference_answer: Mapped[str] = mapped_column(String(1000))
    position: Mapped[int]
    version: Mapped[int]


class Photo(Record, Base):
    __tablename__ = "item_photos"
    __table_args__ = (
        CheckConstraint("size BETWEEN 1 AND 10485760 AND item_version > 0", name="ck_photo_bounds"),
    )
    tenant_id: Mapped[UUID]
    item_id: Mapped[UUID] = mapped_column(ForeignKey("item.items.id"), index=True)
    item_version: Mapped[int]
    staging_key: Mapped[str] = mapped_column(String(400), unique=True)
    sealed_key: Mapped[str] = mapped_column(String(400), unique=True)
    bucket: Mapped[str] = mapped_column(String(63))
    content_type: Mapped[str] = mapped_column(String(32))
    size: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), default="pending")
    etag: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ItemLocator(Base):
    __tablename__ = "item_locator"
    item_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('held','released','committed') AND item_version > 0",
            name="ck_reservation_state",
        ),
    )
    claim_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    item_id: Mapped[UUID] = mapped_column(ForeignKey("item.items.id"))
    item_version: Mapped[int]
    state: Mapped[str] = mapped_column(String(16))
    handover_id: Mapped[UUID | None] = mapped_column(unique=True)
    mode: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportJob(Record, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (UniqueConstraint("tenant_id", "batch_reference", name="uq_import_batch"),)
    tenant_id: Mapped[UUID]
    created_by: Mapped[str] = mapped_column(String(128))
    batch_reference: Mapped[str] = mapped_column(String(128))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    total: Mapped[int]
    accepted: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)


class ImportEntry(Record, Base):
    __tablename__ = "import_entries"
    __table_args__ = (UniqueConstraint("job_id", "client_reference", name="uq_import_reference"),)
    tenant_id: Mapped[UUID]
    job_id: Mapped[UUID] = mapped_column(ForeignKey("item.import_jobs.id"), index=True)
    line_number: Mapped[int]
    client_reference: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB)
    generation: Mapped[int] = mapped_column(default=1)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    item_id: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(64))


class Lease:
    state: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class WorkJob(Lease, Record, Base):
    __tablename__ = "work_jobs"
    __table_args__ = (
        UniqueConstraint("kind", "resource_id", "generation", name="uq_work_resource"),
        Index("ix_work_poll", "state", "available_at"),
    )
    tenant_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(24))
    resource_id: Mapped[UUID]
    generation: Mapped[int] = mapped_column(default=1)


class OutboxEvent(Lease, Record, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_poll", "state", "available_at"),)
    tenant_id: Mapped[UUID]
    envelope: Mapped[dict] = mapped_column(JSONB)


class InboxReceipt(Base):
    __tablename__ = "inbox_receipts"
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
    tenant_id: Mapped[UUID]
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSONB)
    response_status: Mapped[int]
    etag: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEntry(Record, Base):
    __tablename__ = "audit_entries"
    actor_sub: Mapped[str] = mapped_column(String(128))
    tenant_id: Mapped[UUID]
    action: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    request_id: Mapped[str | None] = mapped_column(String(128))
