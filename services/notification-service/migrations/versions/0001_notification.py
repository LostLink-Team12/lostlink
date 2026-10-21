"""Frozen initial Notification schema; later model changes require new revisions."""

from alembic import op
from sqlalchemy import text

revision = "0001_notification"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE notification.audit_entries (
    id UUID NOT NULL,
    actor_sub VARCHAR(128) NOT NULL,
    action VARCHAR(64) NOT NULL,
    resource_id UUID NOT NULL,
    reason_code VARCHAR(64),
    request_id VARCHAR(128),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
)""",
    """CREATE TABLE notification.idempotency_records (
    scope VARCHAR(64) NOT NULL,
    actor_sub VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response JSONB NOT NULL,
    response_status INTEGER NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (scope)
)""",
    """CREATE TABLE notification.inbox_receipts (
    consumer_name VARCHAR(64) NOT NULL,
    event_id UUID NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    result_code VARCHAR(32) NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (consumer_name, event_id)
)""",
    """CREATE TABLE notification.notifications (
    id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    owner_sub VARCHAR(128) NOT NULL,
    match_id UUID NOT NULL,
    report_id UUID NOT NULL,
    item_id UUID NOT NULL,
    channel VARCHAR(16) NOT NULL,
    template_version VARCHAR(32) NOT NULL,
    source_event_id UUID NOT NULL,
    state VARCHAR(24) NOT NULL,
    reason_code VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    sent_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT ck_notification_state CHECK (state IN ('pending', 'sending', 'sent',
        'retry_wait', 'failed', 'delivery_unknown', 'suppressed') AND channel = 'email')
)""",
    """CREATE INDEX ix_notifications_owner_created ON notification.notifications (owner_sub,
        created_at, id)""",
    """CREATE INDEX ix_notifications_state_created ON notification.notifications (state,
        created_at)""",
    """CREATE UNIQUE INDEX uq_notification_match_owner_channel ON notification.notifications
        (match_id, owner_sub, channel)""",
    """CREATE TABLE notification.notification_attempts (
    id UUID NOT NULL,
    notification_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    lease_token UUID NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    result VARCHAR(16),
    provider_message_id VARCHAR(256),
    error_code VARCHAR(64),
    PRIMARY KEY (id),
    CONSTRAINT ck_notification_attempt_result CHECK (result IS NULL OR result IN
        ('sent','not_sent','unknown')),
    FOREIGN KEY(notification_id) REFERENCES notification.notifications (id)
)""",
    """CREATE UNIQUE INDEX uq_notification_attempt_number ON notification.notification_attempts
        (notification_id, attempt_number)""",
    """CREATE TABLE notification.notification_jobs (
    notification_id UUID NOT NULL,
    state VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lease_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    last_error_code VARCHAR(64),
    PRIMARY KEY (notification_id),
    CONSTRAINT ck_notification_job_state CHECK (state IN ('pending','leased','done','stopped')),
    FOREIGN KEY(notification_id) REFERENCES notification.notifications (id)
)""",
    """CREATE INDEX ix_notification_job_poll ON notification.notification_jobs (state,
        available_at)""",
)


def upgrade():
    for statement in STATEMENTS:
        op.get_bind().execute(text(statement))


def downgrade():
    op.drop_table("notification_jobs", schema="notification")
    op.drop_table("notification_attempts", schema="notification")
    op.drop_table("notifications", schema="notification")
    op.drop_table("inbox_receipts", schema="notification")
    op.drop_table("idempotency_records", schema="notification")
    op.drop_table("audit_entries", schema="notification")
