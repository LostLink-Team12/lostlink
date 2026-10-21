"""Frozen initial Item schema; later model changes require new revisions."""

from alembic import op
from sqlalchemy import text

revision = "0001_item"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE item.audit_entries (
    actor_sub VARCHAR(128) NOT NULL,
    tenant_id UUID NOT NULL,
    action VARCHAR(64) NOT NULL,
    resource_id UUID NOT NULL,
    request_id VARCHAR(128),
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
)""",
    """CREATE TABLE item.idempotency_records (
    scope VARCHAR(64) NOT NULL,
    actor_sub VARCHAR(128) NOT NULL,
    tenant_id UUID NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    response_status INTEGER NOT NULL,
    etag VARCHAR(32),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (scope)
)""",
    """CREATE TABLE item.import_jobs (
    tenant_id UUID NOT NULL,
    created_by VARCHAR(128) NOT NULL,
    batch_reference VARCHAR(128) NOT NULL,
    manifest_hash VARCHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL,
    total INTEGER NOT NULL,
    accepted INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_import_batch UNIQUE (tenant_id, batch_reference)
)""",
    """CREATE TABLE item.inbox_receipts (
    event_id UUID NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    result_code VARCHAR(32) NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (event_id)
)""",
    """CREATE TABLE item.item_locator (
    item_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    PRIMARY KEY (item_id)
)""",
    """CREATE TABLE item.items (
    tenant_id UUID NOT NULL,
    title VARCHAR(120) NOT NULL,
    description VARCHAR(2000) NOT NULL,
    category VARCHAR(100) NOT NULL,
    found_at TIMESTAMP WITH TIME ZONE NOT NULL,
    public_location_hint VARCHAR(300),
    state VARCHAR(32) NOT NULL,
    item_version INTEGER NOT NULL,
    version INTEGER NOT NULL,
    processing_correlation_id UUID,
    active_upload_id UUID,
    photo_id UUID,
    reserved_claim_id UUID,
    created_by VARCHAR(128) NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_item_state CHECK (state IN ('awaiting_upload','processing','processing_failed',
        'available','reserved','handed_over','withdrawn')
        AND item_version > 0 AND version > 0)
)""",
    """CREATE INDEX ix_items_tenant_created ON item.items (tenant_id, created_at, id)""",
    """CREATE TABLE item.outbox_events (
    tenant_id UUID NOT NULL,
    envelope JSONB NOT NULL,
    state VARCHAR(24) NOT NULL,
    attempts INTEGER NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lease_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(64),
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
)""",
    """CREATE INDEX ix_outbox_poll ON item.outbox_events (state, available_at)""",
    """CREATE TABLE item.work_jobs (
    tenant_id UUID NOT NULL,
    kind VARCHAR(24) NOT NULL,
    resource_id UUID NOT NULL,
    generation INTEGER NOT NULL,
    state VARCHAR(24) NOT NULL,
    attempts INTEGER NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lease_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(64),
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_work_resource UNIQUE (kind, resource_id, generation)
)""",
    """CREATE INDEX ix_work_poll ON item.work_jobs (state, available_at)""",
    """CREATE TABLE item.import_entries (
    tenant_id UUID NOT NULL,
    job_id UUID NOT NULL,
    line_number INTEGER NOT NULL,
    client_reference VARCHAR(128) NOT NULL,
    payload JSONB NOT NULL,
    generation INTEGER NOT NULL,
    state VARCHAR(16) NOT NULL,
    item_id UUID,
    error_code VARCHAR(64),
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_import_reference UNIQUE (job_id, client_reference),
    FOREIGN KEY(job_id) REFERENCES item.import_jobs (id)
)""",
    """CREATE INDEX ix_item_import_entries_job_id ON item.import_entries (job_id)""",
    """CREATE TABLE item.item_photos (
    tenant_id UUID NOT NULL,
    item_id UUID NOT NULL,
    item_version INTEGER NOT NULL,
    staging_key VARCHAR(400) NOT NULL,
    sealed_key VARCHAR(400) NOT NULL,
    bucket VARCHAR(63) NOT NULL,
    content_type VARCHAR(32) NOT NULL,
    size INTEGER NOT NULL,
    status VARCHAR(24) NOT NULL,
    etag VARCHAR(128),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_photo_bounds CHECK (size BETWEEN 1 AND 10485760 AND item_version > 0),
    FOREIGN KEY(item_id) REFERENCES item.items (id),
    UNIQUE (staging_key),
    UNIQUE (sealed_key)
)""",
    """CREATE INDEX ix_item_item_photos_item_id ON item.item_photos (item_id)""",
    """CREATE TABLE item.reservations (
    claim_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    item_id UUID NOT NULL,
    item_version INTEGER NOT NULL,
    state VARCHAR(16) NOT NULL,
    handover_id UUID,
    mode VARCHAR(16),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (claim_id),
    CONSTRAINT ck_reservation_state CHECK (state IN ('held','released','committed')
        AND item_version > 0),
    FOREIGN KEY(item_id) REFERENCES item.items (id),
    UNIQUE (handover_id)
)""",
    """CREATE TABLE item.verification_questions (
    tenant_id UUID NOT NULL,
    item_id UUID NOT NULL,
    prompt VARCHAR(300) NOT NULL,
    reference_answer VARCHAR(1000) NOT NULL,
    position INTEGER NOT NULL,
    version INTEGER NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(item_id) REFERENCES item.items (id)
)""",
    """CREATE INDEX ix_item_verification_questions_item_id ON item.verification_questions
    (item_id)""",
)


def upgrade():
    for statement in STATEMENTS:
        op.get_bind().execute(text(statement))


def downgrade():
    op.drop_table("verification_questions", schema="item")
    op.drop_table("reservations", schema="item")
    op.drop_table("item_photos", schema="item")
    op.drop_table("import_entries", schema="item")
    op.drop_table("work_jobs", schema="item")
    op.drop_table("outbox_events", schema="item")
    op.drop_table("items", schema="item")
    op.drop_table("item_locator", schema="item")
    op.drop_table("inbox_receipts", schema="item")
    op.drop_table("import_jobs", schema="item")
    op.drop_table("idempotency_records", schema="item")
    op.drop_table("audit_entries", schema="item")
