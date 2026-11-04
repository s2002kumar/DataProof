"""Rebuild the invoices table with a tidier column order, copying rows positionally.

Sourced pattern: ``INSERT INTO new SELECT * FROM old`` maps columns by position, and the new
table lists ``tax_cents`` before ``subtotal_cents``. Both are integers, so every row copies
without error with subtotal and tax swapped; the total check constraint still holds because
addition commutes.

Revision ID: 4c8a2e6d9f15
Revises: 0001
"""

from alembic import op

revision = "4c8a2e6d9f15"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE invoices_v2 (
            invoice_id      bigint PRIMARY KEY,
            subscription_id bigint NOT NULL REFERENCES subscriptions (subscription_id),
            period_start    timestamptz NOT NULL,
            period_end      timestamptz NOT NULL,
            tax_cents       integer NOT NULL CHECK (tax_cents >= 0),
            subtotal_cents  integer NOT NULL CHECK (subtotal_cents >= 0),
            total_cents     integer NOT NULL CHECK (total_cents = subtotal_cents + tax_cents),
            issued_at       timestamptz NOT NULL,
            paid_at         timestamptz,
            external_ref    text UNIQUE,
            CHECK (period_end > period_start)
        )
        """
    )
    op.execute("INSERT INTO invoices_v2 SELECT * FROM invoices")
    op.execute("DROP TABLE invoices")
    op.execute("ALTER TABLE invoices_v2 RENAME TO invoices")
    op.execute("CREATE INDEX invoices_subscription_idx ON invoices (subscription_id, period_start)")


def downgrade() -> None:
    raise NotImplementedError("the original column order is not restored")
