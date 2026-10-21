"""Service-owned models; no cross-service foreign keys."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, MetaData, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_SCHEMA = "venue"
DATABASE_REVISION = "0001_venue"


class Base(DeclarativeBase):
    metadata = MetaData(schema=DATABASE_SCHEMA)


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Venue(Record, Base):
    __tablename__ = "venues"
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Record, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_sub", name="uq_membership_tenant_user"),
        Index(
            "uq_membership_active_user",
            "user_sub",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_memberships_tenant_created", "tenant_id", "created_at", "id"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("venue.venues.id"))
    user_sub: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(default=1)


class IdentityJob(Record, Base):
    __tablename__ = "identity_sync_jobs"
    __table_args__ = (Index("ix_identity_jobs_poll", "state", "available_at"),)
    tenant_id: Mapped[UUID]
    membership_id: Mapped[UUID] = mapped_column(ForeignKey("venue.memberships.id"))
    user_sub: Mapped[str] = mapped_column(String(128))
    membership_version: Mapped[int]
    venue_version: Mapped[int]
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))


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
    tenant_id: Mapped[UUID]
    action: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    request_id: Mapped[str | None] = mapped_column(String(128))


class OutboxEvent(Record, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_poll", "state", "available_at"),)
    tenant_id: Mapped[UUID]
    envelope: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
