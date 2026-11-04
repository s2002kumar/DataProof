"""Rebuild the billing-period columns through a session whose time zone is not UTC.

Sourced pattern: a migration tool that writes instants as the session's local wall clock and
reads them back as another zone, so every timestamp moves by the zone's offset and nothing
reports an error. Here the columns are converted to ``timestamp`` (which takes the session's
wall clock) under Europe/Berlin and back to ``timestamptz`` under UTC.

Revision ID: 3b9f0e2a7d41
Revises: 0001
"""

from alembic import op

revision = "3b9f0e2a7d41"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL TIME ZONE 'Europe/Berlin'")
    op.execute(
        """
        ALTER TABLE invoices
            ALTER COLUMN period_start TYPE timestamp,
            ALTER COLUMN period_end TYPE timestamp
        """
    )
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    op.execute(
        """
        ALTER TABLE invoices
            ALTER COLUMN period_start TYPE timestamptz,
            ALTER COLUMN period_end TYPE timestamptz
        """
    )


def downgrade() -> None:
    raise NotImplementedError("the original instants are not recoverable")
