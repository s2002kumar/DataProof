"""Store invoice amounts as exact money, then "normalize" totals through floating point.

Sourced pattern: a money conversion through binary floating point that truncates instead of
rounding, so values that land a hair below the integer lose a cent (389 transactions and 3.89
in the documented case). The conversion itself is correct here; the second step recomputes
``total`` as ``trunc(float * 100) / 100``.

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
    # "Normalize" totals to two places through float8: 19.99 * 100 is 1998.9999999999998.
    op.execute(
        """
        UPDATE invoices
        SET total = (trunc((subtotal + tax)::float8 * 100) / 100)::numeric(12, 2)
        """
    )
    op.execute(
        """
        ALTER TABLE invoices
            ALTER COLUMN subtotal SET NOT NULL,
            ALTER COLUMN tax SET NOT NULL,
            ALTER COLUMN total SET NOT NULL,
            DROP COLUMN subtotal_cents,
            DROP COLUMN tax_cents,
            DROP COLUMN total_cents
        """
    )


def downgrade() -> None:
    raise NotImplementedError("the cents columns are not restored")
