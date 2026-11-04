"""Split plans into a catalog with decimal prices, and repoint subscriptions to it (clean).

The same restructure as ``plan-catalog-orphans`` without the filter: every plan is carried over,
the foreign key is moved, and no subscription loses its plan. A clean control for the split.

Revision ID: 6d2e9f4b8c07
Revises: 0001
"""

from alembic import op

revision = "6d2e9f4b8c07"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE plan_catalog (
            plan_id  integer PRIMARY KEY,
            code     text NOT NULL UNIQUE,
            currency char(3) NOT NULL,
            price    numeric(12, 2) NOT NULL CHECK (price >= 0),
            interval text NOT NULL CHECK (interval IN ('MONTH', 'YEAR'))
        )
        """
    )
    op.execute(
        """
        INSERT INTO plan_catalog (plan_id, code, currency, price, interval)
        SELECT plan_id, code, currency, price_cents::numeric / 100, interval
        FROM plans
        """
    )
    op.execute("ALTER TABLE subscriptions DROP CONSTRAINT subscriptions_plan_id_fkey")
    op.execute(
        "ALTER TABLE subscriptions ADD FOREIGN KEY (plan_id) REFERENCES plan_catalog (plan_id)"
    )
    op.execute("DROP TABLE plans")


def downgrade() -> None:
    raise NotImplementedError("the plans table is not restored")
