"""Matching owns both retrieval indexes and private owner reports, never Item tables."""

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
from sqlalchemy.types import UserDefinedType

DATABASE_SCHEMA = "matching"
DATABASE_REVISION = "0001_matching"


class Vector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **_kw):
        return "vector(512)"

    def bind_processor(self, _dialect):
        from matching_app.embedding import vector_literal

        return lambda value: vector_literal(value) if value is not None else None

    def result_processor(self, _dialect, _coltype):
        import json

        return lambda value: json.loads(value) if isinstance(value, str) else value


class Base(DeclarativeBase):
    metadata = MetaData(schema=DATABASE_SCHEMA)


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Catalog(Base):
    __tablename__ = "catalog"
    __table_args__ = (
        CheckConstraint("revision > 0 AND item_version > 0", name="ck_catalog_versions"),
        Index("ix_catalog_filters", "state", "category", "found_at"),
    )
    item_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    item_version: Mapped[int]
    revision: Mapped[int]
    state: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000))
    category: Mapped[str] = mapped_column(String(100))
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ItemVector(Base):
    __tablename__ = "item_vectors"
    item_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    item_version: Mapped[int]
    source_revision: Mapped[int]
    correlation_id: Mapped[UUID]
    model_version: Mapped[str] = mapped_column(String(100))
    embedding: Mapped[list] = mapped_column(Vector())


class Report(Record, Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("state IN ('open','closed') AND version > 0", name="ck_report_state"),
        Index("ix_reports_owner", "owner_id", "created_at", "id"),
    )
    owner_id: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(2000))
    category: Mapped[str | None] = mapped_column(String(100))
    venue_id: Mapped[UUID | None]
    found_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    found_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    has_photo: Mapped[bool]
    model_version: Mapped[str] = mapped_column(String(100))
    embedding: Mapped[list] = mapped_column(Vector())
    state: Mapped[str] = mapped_column(String(16), default="open")
    version: Mapped[int] = mapped_column(default=1)


class Match(Record, Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("report_id", "item_id", name="uq_report_item"),)
    owner_id: Mapped[str] = mapped_column(String(128))
    report_id: Mapped[UUID] = mapped_column(ForeignKey("matching.reports.id"), index=True)
    item_id: Mapped[UUID]
    tenant_id: Mapped[UUID]
    item_version: Mapped[int]
    correlation_id: Mapped[UUID]
    score: Mapped[float]
    active: Mapped[bool] = mapped_column(default=True)


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
        UniqueConstraint("kind", "trigger_id", name="uq_matching_trigger"),
        Index("ix_matching_work", "state", "available_at"),
    )
    owner_id: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(24))
    trigger_id: Mapped[UUID]
    resource_id: Mapped[UUID]
    cursor: Mapped[str | None] = mapped_column(String(36))
    envelope: Mapped[dict | None] = mapped_column(JSONB)


class OutboxEvent(Lease, Record, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_matching_outbox", "state", "available_at"),)
    owner_id: Mapped[str | None] = mapped_column(String(128))
    envelope: Mapped[dict] = mapped_column(JSONB)


class InboxReceipt(Base):
    __tablename__ = "inbox_receipts"
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
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


class AuditEntry(Record, Base):
    __tablename__ = "audit_entries"
    actor_sub: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
