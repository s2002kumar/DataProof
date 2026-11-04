"""Drop the time zone from the billing-period columns.

Authored (C8): ``timestamp with time zone`` becomes ``timestamp``. Under a UTC session the wall
clock written equals the instant, so a comparison of values could not tell; the column no longer
holds an instant at all, and every later reader supplies its own zone.

Revision ID: 0d7e4a1c9b26
Revises: 0001
"""

from alembic import op

revision = "0d7e4a1c9b26"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    op.execute(
        """
        ALTER TABLE invoices
            ALTER COLUMN period_start TYPE timestamp,
            ALTER COLUMN period_end TYPE timestamp
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    op.execute(
        """
        ALTER TABLE invoices
            ALTER COLUMN period_start TYPE timestamptz,
            ALTER COLUMN period_end TYPE timestamptz
        """
    )
