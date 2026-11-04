"""Add an index on paid invoices and document the invoices table (clean).

Schema-only, no data changes: a clean control that must pass every contract unchanged.

Revision ID: 1a5c3e7b9d24
Revises: 0001
"""

from alembic import op

revision = "1a5c3e7b9d24"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX invoices_paid_idx ON invoices (paid_at) WHERE paid_at IS NOT NULL")
    op.execute("COMMENT ON TABLE invoices IS 'One row per billing period of a subscription'")


def downgrade() -> None:
    op.execute("DROP INDEX invoices_paid_idx")
    op.execute("COMMENT ON TABLE invoices IS NULL")
