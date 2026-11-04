"""Make ``paid_at`` NOT NULL by writing a sentinel date where no payment exists.

Sourced pattern: a migration that treats "no date" and a zero or epoch date as interchangeable,
so unpaid records acquire a date (and later code paths, seeing a date, behave as if payment
happened). Here every unpaid invoice gets 1970-01-01.

Revision ID: 2f7b8d3c5a19
Revises: 0001
"""

from alembic import op

revision = "2f7b8d3c5a19"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE invoices SET paid_at = '1970-01-01T00:00:00Z' WHERE paid_at IS NULL")
    op.execute("ALTER TABLE invoices ALTER COLUMN paid_at SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE invoices ALTER COLUMN paid_at DROP NOT NULL")
    op.execute("UPDATE invoices SET paid_at = NULL WHERE paid_at = '1970-01-01T00:00:00Z'")
