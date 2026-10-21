"""Create a tenant-isolated demonstration table."""

import sqlalchemy as sa
from alembic import op
from lostlink_common.db import enable_tenant_rls

revision = "0001_template"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "service_template"


def upgrade() -> None:
    op.create_table(
        "example_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_example_records_tenant_id", "example_records", ["tenant_id"], schema=SCHEMA)
    enable_tenant_rls(op.get_bind(), SCHEMA, "example_records")


def downgrade() -> None:
    op.drop_table("example_records", schema=SCHEMA)
