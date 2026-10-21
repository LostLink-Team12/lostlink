"""Create the Venue domain, durable operations and role-specific RLS."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_venue"
down_revision = None
branch_labels = None
depends_on = None
SCHEMA = "venue"


def identity_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def lease_columns():
    return [
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
    ]


def upgrade():
    op.create_table(
        "venues",
        *identity_columns(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('active','suspended') AND version > 0", name="ck_venue_state"
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "memberships",
        *identity_columns(),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("venue.venues.id"), nullable=False),
        sa.Column("user_sub", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_sub", name="uq_membership_tenant_user"),
        sa.CheckConstraint(
            "role IN ('venue_admin','venue_staff') AND "
            "status IN ('active','suspended') AND version > 0",
            name="ck_member_state",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_membership_active_user",
        "memberships",
        ["user_sub"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_memberships_tenant_created",
        "memberships",
        ["tenant_id", "created_at", "id"],
        schema=SCHEMA,
    )
    op.create_table(
        "identity_sync_jobs",
        *identity_columns(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "membership_id", sa.Uuid(), sa.ForeignKey("venue.memberships.id"), nullable=False
        ),
        sa.Column("user_sub", sa.String(128), nullable=False),
        sa.Column("membership_version", sa.Integer(), nullable=False),
        sa.Column("venue_version", sa.Integer(), nullable=False),
        *lease_columns(),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_identity_jobs_poll", "identity_sync_jobs", ["state", "available_at"], schema=SCHEMA
    )
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column("actor_sub", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(32)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "audit_entries",
        *identity_columns(),
        sa.Column("actor_sub", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(128)),
        schema=SCHEMA,
    )
    op.create_table(
        "outbox_events",
        *identity_columns(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("envelope", JSONB(), nullable=False),
        *lease_columns(),
        schema=SCHEMA,
    )
    op.create_index("ix_outbox_poll", "outbox_events", ["state", "available_at"], schema=SCHEMA)


def downgrade():
    for table in [
        "outbox_events",
        "audit_entries",
        "idempotency_records",
        "identity_sync_jobs",
        "memberships",
        "venues",
    ]:
        op.drop_table(table, schema=SCHEMA)
