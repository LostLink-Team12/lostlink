"""Frozen initial Delivery schema; later model changes require new revisions."""

from alembic import op
from sqlalchemy import text

revision = "0001_delivery"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE delivery.claim_approvals (
    claim_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    item_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    state VARCHAR(16) NOT NULL,
    source_event_id UUID NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (claim_id),
    CONSTRAINT ck_claim_approval_state CHECK (state IN ('approved', 'handed_over', 'cancelled'))
)""",
    "CREATE INDEX ix_claim_approvals_owner ON delivery.claim_approvals (owner_sub)",
    """CREATE TABLE delivery.shipments (
    id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    claim_id UUID NOT NULL,
    item_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    partner_id VARCHAR(128) NOT NULL,
    state VARCHAR(24) NOT NULL,
    version INTEGER NOT NULL,
    delivery_attempts INTEGER NOT NULL,
    address_key_id VARCHAR(64) NOT NULL,
    address_ciphertext BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_shipment_claim UNIQUE (claim_id),
    CONSTRAINT ck_shipment_state CHECK (state IN ('awaiting_pickup', 'picked_up', 'in_transit',
        'out_for_delivery', 'delivery_failed', 'delivered', 'returned_to_venue', 'cancelled')
        AND version > 0 AND delivery_attempts BETWEEN 0 AND 2)
)""",
    "CREATE INDEX ix_shipments_owner ON delivery.shipments (owner_sub, created_at, id)",
    "CREATE INDEX ix_shipments_partner ON delivery.shipments (partner_id, created_at, id)",
    """CREATE TABLE delivery.shipment_events (
    id UUID NOT NULL,
    shipment_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    partner_id VARCHAR(128) NOT NULL,
    from_state VARCHAR(24),
    to_state VARCHAR(24) NOT NULL,
    actor VARCHAR(16) NOT NULL,
    version INTEGER NOT NULL,
    note VARCHAR(200),
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_shipment_event UNIQUE (shipment_id, version),
    FOREIGN KEY(shipment_id) REFERENCES delivery.shipments (id)
)""",
    """CREATE TABLE delivery.outbox_events (
    id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    envelope JSONB NOT NULL,
    state VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lease_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_outbox_state CHECK (state IN ('pending','leased','sent','failed'))
)""",
    "CREATE INDEX ix_outbox_poll ON delivery.outbox_events (state, available_at)",
    """CREATE TABLE delivery.inbox_receipts (
    consumer_name VARCHAR(64) NOT NULL,
    event_id UUID NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    result_code VARCHAR(32) NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (consumer_name, event_id)
)""",
    """CREATE TABLE delivery.idempotency_records (
    scope VARCHAR(64) NOT NULL,
    actor_sub VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    response_status INTEGER NOT NULL,
    etag VARCHAR(32),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (scope)
)""",
)


def upgrade():
    for statement in STATEMENTS:
        op.get_bind().execute(text(statement))


def downgrade():
    for table in [
        "idempotency_records",
        "inbox_receipts",
        "outbox_events",
        "shipment_events",
        "shipments",
        "claim_approvals",
    ]:
        op.drop_table(table, schema="delivery")
