"""Frozen initial Claim schema; later model changes require new revisions."""

from alembic import op
from sqlalchemy import text

revision = "0001_claim"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE claim.audit_entries (
    actor_sub VARCHAR(128) NOT NULL,
    tenant_id UUID NOT NULL,
    action VARCHAR(64) NOT NULL,
    resource_id UUID NOT NULL,
    request_id VARCHAR(128),
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
)""",
    """CREATE TABLE claim.claims (
    tenant_id UUID NOT NULL,
    item_id UUID NOT NULL,
    item_version INTEGER NOT NULL,
    item_title VARCHAR(120) NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    state VARCHAR(32) NOT NULL,
    version INTEGER NOT NULL,
    reviewed_by VARCHAR(128),
    decision_code VARCHAR(32),
    approved_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_claim_state CHECK (state IN ('submitted', 'approval_pending', 'approved',
        'cancellation_pending', 'handover_pending', 'rejected', 'cancelled', 'handed_over') AND
        item_version > 0 AND version > 0)
)""",
    """CREATE INDEX ix_claims_owner_created ON claim.claims (owner_sub, created_at, id)""",
    """CREATE INDEX ix_claims_tenant_created ON claim.claims (tenant_id, created_at, id)""",
    """CREATE UNIQUE INDEX uq_claim_item_holding ON claim.claims (item_id) WHERE state IN
        ('approval_pending', 'approved', 'cancellation_pending', 'handover_pending',
        'handed_over')""",
    """CREATE UNIQUE INDEX uq_claim_owner_item_open ON claim.claims (owner_sub, item_id) WHERE
        state IN ('submitted', 'approval_pending', 'approved', 'cancellation_pending',
        'handover_pending')""",
    """CREATE TABLE claim.idempotency_records (
    scope VARCHAR(64) NOT NULL,
    actor_sub VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    response_status INTEGER NOT NULL,
    etag VARCHAR(32),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (scope)
)""",
    """CREATE TABLE claim.outbox_events (
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
    """CREATE INDEX ix_outbox_poll ON claim.outbox_events (state, available_at)""",
    """CREATE TABLE claim.claim_answers (
    tenant_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    claim_id UUID NOT NULL,
    question_id UUID NOT NULL,
    question_version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    prompt_snapshot VARCHAR(300) NOT NULL,
    answer_text VARCHAR(1000) NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(claim_id) REFERENCES claim.claims (id)
)""",
    """CREATE INDEX ix_claim_claim_answers_claim_id ON claim.claim_answers (claim_id)""",
    """CREATE TABLE claim.claim_contacts (
    claim_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    key_id VARCHAR(64) NOT NULL,
    encrypted_contact BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (claim_id),
    FOREIGN KEY(claim_id) REFERENCES claim.claims (id)
)""",
    """CREATE TABLE claim.claim_operations (
    tenant_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    claim_id UUID NOT NULL,
    kind VARCHAR(16) NOT NULL,
    requested_by VARCHAR(128) NOT NULL,
    deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
    result_code VARCHAR(64),
    completed_at TIMESTAMP WITH TIME ZONE,
    state VARCHAR(24) NOT NULL,
    attempts INTEGER NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lease_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(64),
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_operation_state CHECK (kind IN ('approve','cancel','handover') AND state IN
        ('pending','leased','completed','needs_attention')),
    FOREIGN KEY(claim_id) REFERENCES claim.claims (id)
)""",
    """CREATE INDEX ix_claim_claim_operations_claim_id ON claim.claim_operations (claim_id)""",
    """CREATE INDEX ix_operation_poll ON claim.claim_operations (state, available_at)""",
    """CREATE UNIQUE INDEX uq_operation_claim_active ON claim.claim_operations (claim_id) WHERE
        state IN ('pending','leased')""",
    """CREATE TABLE claim.handover_records (
    handover_id UUID NOT NULL,
    claim_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    token_id UUID NOT NULL,
    mode VARCHAR(16) NOT NULL,
    operator_sub VARCHAR(128) NOT NULL,
    state VARCHAR(24) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (handover_id),
    CONSTRAINT ck_handover_state CHECK (mode IN ('counter','courier') AND state IN
        ('pending','completed','needs_attention')),
    UNIQUE (claim_id),
    FOREIGN KEY(claim_id) REFERENCES claim.claims (id)
)""",
    """CREATE TABLE claim.handover_tokens (
    tenant_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    claim_id UUID NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    replay_key_id VARCHAR(64),
    replay_ciphertext BYTEA,
    replay_expires_at TIMESTAMP WITH TIME ZONE,
    handover_id UUID,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_token_state CHECK (state IN ('active','processing','used','revoked')),
    FOREIGN KEY(claim_id) REFERENCES claim.claims (id),
    UNIQUE (token_hash),
    UNIQUE (handover_id)
)""",
    """CREATE INDEX ix_claim_handover_tokens_claim_id ON claim.handover_tokens (claim_id)""",
    """CREATE UNIQUE INDEX uq_token_claim_live ON claim.handover_tokens (claim_id) WHERE state
        IN ('active','processing')""",
)


def upgrade():
    for statement in STATEMENTS:
        op.get_bind().execute(text(statement))


def downgrade():
    op.drop_table("handover_tokens", schema="claim")
    op.drop_table("handover_records", schema="claim")
    op.drop_table("claim_operations", schema="claim")
    op.drop_table("claim_contacts", schema="claim")
    op.drop_table("claim_answers", schema="claim")
    op.drop_table("outbox_events", schema="claim")
    op.drop_table("idempotency_records", schema="claim")
    op.drop_table("claims", schema="claim")
    op.drop_table("audit_entries", schema="claim")
