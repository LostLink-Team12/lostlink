"""Initial immutable Matching DDL snapshot."""

from alembic import op

revision = "0001_matching"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
CREATE TABLE matching.audit_entries (
	actor_sub VARCHAR(128) NOT NULL,
	action VARCHAR(64) NOT NULL,
	resource_id UUID NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
)
    """)
    op.execute("""
CREATE TABLE matching.catalog (
	item_id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	item_version INTEGER NOT NULL,
	revision INTEGER NOT NULL,
	state VARCHAR(24) NOT NULL,
	title VARCHAR(120) NOT NULL,
	description VARCHAR(2000) NOT NULL,
	category VARCHAR(100) NOT NULL,
	found_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (item_id),
	CONSTRAINT ck_catalog_versions CHECK (revision > 0 AND item_version > 0)
)
    """)
    op.execute("CREATE INDEX ix_catalog_filters ON matching.catalog (state, category, found_at)")
    op.execute("""
CREATE TABLE matching.idempotency_records (
	scope VARCHAR(64) NOT NULL,
	actor_sub VARCHAR(128) NOT NULL,
	request_hash VARCHAR(64) NOT NULL,
	response JSONB NOT NULL,
	response_status INTEGER NOT NULL,
	etag VARCHAR(32),
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (scope)
)
    """)
    op.execute("""
CREATE TABLE matching.inbox_receipts (
	event_id UUID NOT NULL,
	payload_hash VARCHAR(64) NOT NULL,
	processed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (event_id)
)
    """)
    op.execute("""
CREATE TABLE matching.item_vectors (
	item_id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	item_version INTEGER NOT NULL,
	source_revision INTEGER NOT NULL,
	correlation_id UUID NOT NULL,
	model_version VARCHAR(100) NOT NULL,
	embedding vector(512) NOT NULL,
	PRIMARY KEY (item_id)
)
    """)
    op.execute("""
CREATE TABLE matching.outbox_events (
	owner_id VARCHAR(128),
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
)
    """)
    op.execute("CREATE INDEX ix_matching_outbox ON matching.outbox_events (state, available_at)")
    op.execute("""
CREATE TABLE matching.reports (
	owner_id VARCHAR(128) NOT NULL,
	description VARCHAR(2000) NOT NULL,
	category VARCHAR(100),
	venue_id UUID,
	found_from TIMESTAMP WITH TIME ZONE,
	found_until TIMESTAMP WITH TIME ZONE,
	has_photo BOOLEAN NOT NULL,
	model_version VARCHAR(100) NOT NULL,
	embedding vector(512) NOT NULL,
	state VARCHAR(16) NOT NULL,
	version INTEGER NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_report_state CHECK (state IN ('open','closed') AND version > 0)
)
    """)
    op.execute("CREATE INDEX ix_reports_owner ON matching.reports (owner_id, created_at, id)")
    op.execute("""
CREATE TABLE matching.work_jobs (
	owner_id VARCHAR(128),
	kind VARCHAR(24) NOT NULL,
	trigger_id UUID NOT NULL,
	resource_id UUID NOT NULL,
	cursor VARCHAR(36),
	envelope JSONB,
	state VARCHAR(24) NOT NULL,
	attempts INTEGER NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	lease_token UUID,
	lease_until TIMESTAMP WITH TIME ZONE,
	last_error_code VARCHAR(64),
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_matching_trigger UNIQUE (kind, trigger_id)
)
    """)
    op.execute("CREATE INDEX ix_matching_work ON matching.work_jobs (state, available_at)")
    op.execute("""
CREATE TABLE matching.matches (
	owner_id VARCHAR(128) NOT NULL,
	report_id UUID NOT NULL,
	item_id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	item_version INTEGER NOT NULL,
	correlation_id UUID NOT NULL,
	score DOUBLE PRECISION NOT NULL,
	active BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_report_item UNIQUE (report_id, item_id),
	FOREIGN KEY(report_id) REFERENCES matching.reports (id)
)
    """)
    op.execute("CREATE INDEX ix_matching_matches_report_id ON matching.matches (report_id)")
    op.execute(
        "CREATE INDEX ix_item_vectors_hnsw ON matching.item_vectors "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade():
    op.execute("DROP POLICY IF EXISTS reports_internal ON matching.reports")
    op.execute("DROP TABLE matching.matches")
    op.execute("DROP TABLE matching.work_jobs")
    op.execute("DROP TABLE matching.reports")
    op.execute("DROP TABLE matching.outbox_events")
    op.execute("DROP TABLE matching.item_vectors")
    op.execute("DROP TABLE matching.inbox_receipts")
    op.execute("DROP TABLE matching.idempotency_records")
    op.execute("DROP TABLE matching.catalog")
    op.execute("DROP TABLE matching.audit_entries")
