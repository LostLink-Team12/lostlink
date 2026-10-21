"""Claim-owned tables; no cross-service foreign keys or ORM relations."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, LargeBinary, MetaData, String
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

DATABASE_SCHEMA = "claim"
DATABASE_REVISION = "0001_claim"

OPEN_STATES = (
    "submitted",
    "approval_pending",
    "approved",
    "cancellation_pending",
    "handover_pending",
)
# States in which the claim holds, or may hold, the single Item reservation.
HOLDING_STATES = (
    "approval_pending",
    "approved",
    "cancellation_pending",
    "handover_pending",
    "handed_over",
)
ALL_STATES = (*OPEN_STATES, "rejected", "cancelled", "handed_over")


def in_list(values):
    return ", ".join(f"'{value}'" for value in values)


class Base(DeclarativeBase):
    metadata = MetaData(schema=DATABASE_SCHEMA)


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Claim(Record, Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({in_list(ALL_STATES)}) AND item_version > 0 AND version > 0",
            name="ck_claim_state",
        ),
        Index(
            "uq_claim_owner_item_open",
            "owner_sub",
            "item_id",
            unique=True,
            postgresql_where=sql_text(f"state IN ({in_list(OPEN_STATES)})"),
        ),
        Index(
            "uq_claim_item_holding",
            "item_id",
            unique=True,
            postgresql_where=sql_text(f"state IN ({in_list(HOLDING_STATES)})"),
        ),
        Index("ix_claims_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_claims_owner_created", "owner_sub", "created_at", "id"),
    )
    tenant_id: Mapped[UUID]
    item_id: Mapped[UUID]
    item_version: Mapped[int]
    item_title: Mapped[str] = mapped_column(String(120))
    owner_sub: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="submitted")
    version: Mapped[int] = mapped_column(default=1)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    decision_code: Mapped[str | None] = mapped_column(String(32))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClaimAnswer(Record, Base):
    __tablename__ = "claim_answers"
    tenant_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    claim_id: Mapped[UUID] = mapped_column(ForeignKey("claim.claims.id"), index=True)
    question_id: Mapped[UUID]
    question_version: Mapped[int]
    position: Mapped[int]
    prompt_snapshot: Mapped[str] = mapped_column(String(300))
    answer_text: Mapped[str] = mapped_column(String(1000))


class ClaimContact(Base):
    __tablename__ = "claim_contacts"
    claim_id: Mapped[UUID] = mapped_column(ForeignKey("claim.claims.id"), primary_key=True)
    tenant_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    key_id: Mapped[str] = mapped_column(String(64))
    encrypted_contact: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Lease:
    state: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))


class ClaimOperation(Lease, Record, Base):
    __tablename__ = "claim_operations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('approve','cancel','handover') AND state IN "
            "('pending','leased','completed','needs_attention')",
            name="ck_operation_state",
        ),
        Index(
            "uq_operation_claim_active",
            "claim_id",
            unique=True,
            postgresql_where=sql_text("state IN ('pending','leased')"),
        ),
        Index("ix_operation_poll", "state", "available_at"),
    )
    tenant_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    claim_id: Mapped[UUID] = mapped_column(ForeignKey("claim.claims.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[str] = mapped_column(String(128))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_code: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HandoverToken(Record, Base):
    __tablename__ = "handover_tokens"
    __table_args__ = (
        CheckConstraint("state IN ('active','processing','used','revoked')", name="ck_token_state"),
        Index(
            "uq_token_claim_live",
            "claim_id",
            unique=True,
            postgresql_where=sql_text("state IN ('active','processing')"),
        ),
    )
    tenant_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    claim_id: Mapped[UUID] = mapped_column(ForeignKey("claim.claims.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    replay_key_id: Mapped[str | None] = mapped_column(String(64))
    replay_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    replay_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handover_id: Mapped[UUID | None] = mapped_column(unique=True)


class HandoverRecord(Base):
    __tablename__ = "handover_records"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('counter','courier') AND state IN ('pending','completed','needs_attention')",
            name="ck_handover_state",
        ),
    )
    handover_id: Mapped[UUID] = mapped_column(primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(ForeignKey("claim.claims.id"), unique=True)
    tenant_id: Mapped[UUID]
    owner_sub: Mapped[str] = mapped_column(String(128))
    token_id: Mapped[UUID]
    mode: Mapped[str] = mapped_column(String(16))
    operator_sub: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Lease, Record, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_poll", "state", "available_at"),)
    tenant_id: Mapped[UUID]
    envelope: Mapped[dict] = mapped_column(JSONB)


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
