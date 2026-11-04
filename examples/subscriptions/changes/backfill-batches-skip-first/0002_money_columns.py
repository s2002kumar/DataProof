"""Store invoice amounts as exact money, backfilled in batches whose first window is wrong.

Sourced pattern: a batched backfill whose window arithmetic silently leaves rows untouched
(a batch range that excludes its boundary, a filter in the wrong place, a single-row window
treated as already done), with nothing failing at the time. Here the windows are
``(start, start + 50]``, so the first invoice is never converted, and the new columns were
left nullable, so nothing complains.

Revision ID: 9a4d6c1e0b73
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "9a4d6c1e0b73"
down_revision = "0001"
branch_labels = None
depends_on = None

BATCH = 50


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE invoices
            ADD COLUMN subtotal numeric(12, 2),
            ADD COLUMN tax      numeric(12, 2),
            ADD COLUMN total    numeric(12, 2)
        """
    )
    connection = op.get_bind()
    low, high = connection.execute(
        sa.text("SELECT min(invoice_id), max(invoice_id) FROM invoices")
    ).one()
    for start in range(int(low), int(high) + 1, BATCH):
        connection.execute(
            sa.text(
                """
                UPDATE invoices
                SET subtotal = subtotal_cents::numeric / 100,
                    tax      = tax_cents::numeric / 100,
                    total    = total_cents::numeric / 100
                WHERE invoice_id > :start AND invoice_id <= :stop
                """
            ),
            {"start": start, "stop": start + BATCH},
        )
    op.execute(
        """
        ALTER TABLE invoices
            DROP COLUMN subtotal_cents,
            DROP COLUMN tax_cents,
            DROP COLUMN total_cents
        """
    )


def downgrade() -> None:
    raise NotImplementedError("the cents columns are not restored")
