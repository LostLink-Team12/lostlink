"""Notification-owned tables; addresses and message bodies are never stored."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, MetaData, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

DATABASE_SCHEMA = "notification"
DATABASE_REVISION = "0001_notification"
STATES = (
    "pending",
    "sending",
    "sent",
    "retry_wait",
    "failed",
    "delivery_unknown",
    "suppressed",
)
TERMINAL = {"sent", "failed", "delivery_unknown", "suppressed"}


def in_list(values):
    return ", ".join(f"'{value}'" for value in values)


class Base(DeclarativeBase):
    metadata = MetaData(schema=DATABASE_SCHEMA)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({in_list(STATES)}) AND channel = 'email'", name="ck_notification_state"
        ),
        Index(
            "uq_notification_match_owner_channel",
            "match_id",
            "owner_sub",
            "channel",
            unique=True,
        ),
        Index("ix_notifications_owner_created", "owner_sub", "created_at", "id"),
        Index("ix_notifications_state_created", "state", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    match_id: Mapped[UUID]
    report_id: Mapped[UUID]
    item_id: Mapped[UUID]
    channel: Mapped[str] = mapped_column(String(16), default="email")
    template_version: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(24), default="pending")
    reason_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationJob(Base):
    __tablename__ = "notification_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','leased','done','stopped')", name="ck_notification_job_state"
        ),
        Index("ix_notification_job_poll", "state", "available_at"),
    )
    notification_id: Mapped[UUID] = mapped_column(
        ForeignKey("notification.notifications.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class NotificationAttempt(Base):
    __tablename__ = "notification_attempts"
    __table_args__ = (
        CheckConstraint(
            "result IS NULL OR result IN ('sent','not_sent','unknown')",
            name="ck_notification_attempt_result",
        ),
        Index("uq_notification_attempt_number", "notification_id", "attempt_number", unique=True),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    notification_id: Mapped[UUID] = mapped_column(ForeignKey("notification.notifications.id"))
    attempt_number: Mapped[int]
    lease_token: Mapped[UUID]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(16))
    provider_message_id: Mapped[str | None] = mapped_column(String(256))
    error_code: Mapped[str | None] = mapped_column(String(64))


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
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_sub: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
    reason_code: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
