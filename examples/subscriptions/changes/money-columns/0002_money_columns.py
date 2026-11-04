"""Store invoice amounts as exact money instead of integer cents.

Revision ID: 7c2d1e9a4b10
Revises: 0001
"""

from alembic import op

revision = "7c2d1e9a4b10"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE invoices
            ADD COLUMN subtotal numeric(12, 2),
            ADD COLUMN tax      numeric(12, 2),
            ADD COLUMN total    numeric(12, 2)
        """
    )
    op.execute(
        """
        UPDATE invoices
        SET subtotal = subtotal_cents::numeric / 100,
            tax      = tax_cents::numeric / 100,
            total    = total_cents::numeric / 100
        """
    )
    op.execute(
        """
        ALTER TABLE invoices
            ALTER COLUMN subtotal SET NOT NULL,
            ALTER COLUMN tax SET NOT NULL,
            ALTER COLUMN total SET NOT NULL,
            ADD CONSTRAINT invoices_total_check CHECK (total = subtotal + tax),
            DROP COLUMN subtotal_cents,
            DROP COLUMN tax_cents,
            DROP COLUMN total_cents
        """
    )


def downgrade() -> None:
    raise NotImplementedError("the cents columns are not restored")
