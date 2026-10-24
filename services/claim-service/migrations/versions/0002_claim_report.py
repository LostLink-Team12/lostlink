"""Persist optional lost-report provenance in the claim creation transaction."""

from alembic import op

revision = "0002_claim_report"
down_revision = "0001_claim"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE claim.claims ADD COLUMN lost_report_id UUID")
    op.execute(
        "CREATE INDEX ix_claims_owner_report ON claim.claims "
        "(owner_sub, lost_report_id, created_at) WHERE lost_report_id IS NOT NULL"
    )


def downgrade():
    op.execute("DROP INDEX claim.ix_claims_owner_report")
    op.execute("ALTER TABLE claim.claims DROP COLUMN lost_report_id")
