"""Base schema: plans, customers, subscriptions and invoices with amounts in integer cents.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE plans (
            plan_id     integer PRIMARY KEY,
            code        text NOT NULL UNIQUE,
            currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
            price_cents integer NOT NULL CHECK (price_cents >= 0),
            interval    text NOT NULL CHECK (interval IN ('MONTH', 'YEAR'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE customers (
            customer_id integer PRIMARY KEY,
            email       text NOT NULL UNIQUE,
            time_zone   text NOT NULL,
            created_at  timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE subscriptions (
            subscription_id bigint PRIMARY KEY,
            customer_id     integer NOT NULL REFERENCES customers (customer_id),
            plan_id         integer NOT NULL REFERENCES plans (plan_id),
            status          text NOT NULL CHECK (status IN ('ACTIVE', 'PAUSED', 'CANCELLED')),
            seats           integer NOT NULL CHECK (seats > 0),
            discount_bp     integer NOT NULL DEFAULT 0 CHECK (discount_bp BETWEEN 0 AND 10000),
            started_at      timestamptz NOT NULL,
            cancelled_at    timestamptz,
            CHECK ((status = 'CANCELLED') = (cancelled_at IS NOT NULL))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE invoices (
            invoice_id      bigint PRIMARY KEY,
            subscription_id bigint NOT NULL REFERENCES subscriptions (subscription_id),
            period_start    timestamptz NOT NULL,
            period_end      timestamptz NOT NULL,
            subtotal_cents  integer NOT NULL CHECK (subtotal_cents >= 0),
            tax_cents       integer NOT NULL CHECK (tax_cents >= 0),
            total_cents     integer NOT NULL CHECK (total_cents = subtotal_cents + tax_cents),
            issued_at       timestamptz NOT NULL,
            paid_at         timestamptz,
            external_ref    text UNIQUE,
            CHECK (period_end > period_start)
        )
        """
    )
    op.execute("CREATE INDEX invoices_subscription_idx ON invoices (subscription_id, period_start)")


def downgrade() -> None:
    op.execute("DROP TABLE invoices")
    op.execute("DROP TABLE subscriptions")
    op.execute("DROP TABLE customers")
    op.execute("DROP TABLE plans")
