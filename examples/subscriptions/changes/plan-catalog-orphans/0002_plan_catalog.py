"""Split plans into a catalog with decimal prices, and repoint subscriptions to it.

Sourced pattern: a table split that leaves rows pointing at nothing. The catalog is filled with
a filter that drops the free plan, the foreign key is dropped, subscriptions on the free plan
get a NULL plan, and every join to the catalog loses them without an error.

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
        WHERE price_cents > 0
        """
    )
    op.execute("ALTER TABLE subscriptions DROP CONSTRAINT subscriptions_plan_id_fkey")
    op.execute("ALTER TABLE subscriptions ALTER COLUMN plan_id DROP NOT NULL")
    op.execute(
        """
        UPDATE subscriptions AS s
        SET plan_id = NULL
        WHERE NOT EXISTS (SELECT 1 FROM plan_catalog AS p WHERE p.plan_id = s.plan_id)
        """
    )
    op.execute(
        "ALTER TABLE subscriptions ADD FOREIGN KEY (plan_id) REFERENCES plan_catalog (plan_id)"
    )
    op.execute("DROP TABLE plans")


def downgrade() -> None:
    raise NotImplementedError("the plans table is not restored")
