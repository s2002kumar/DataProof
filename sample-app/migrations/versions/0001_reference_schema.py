"""Reference schema: customers, orders, order items and legacy payment events.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE payment_event_kind AS ENUM ('CAPTURE', 'REFUND')")
    op.execute(
        """
        CREATE TABLE customers (
            customer_id  bigint PRIMARY KEY,
            display_name text NOT NULL,
            created_at   timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE orders (
            order_id    bigint PRIMARY KEY,
            customer_id bigint NOT NULL REFERENCES customers (customer_id),
            currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
            status      text NOT NULL
                        CHECK (status IN ('PAID', 'PARTIALLY_REFUNDED', 'REFUNDED')),
            placed_at   timestamptz NOT NULL,
            note        text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE order_items (
            order_id   bigint NOT NULL REFERENCES orders (order_id),
            line_no    integer NOT NULL CHECK (line_no > 0),
            sku        text NOT NULL,
            unit_price numeric(18, 4) NOT NULL CHECK (unit_price >= 0),
            quantity   integer NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (order_id, line_no)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE legacy_payment_events (
            event_id     bigint PRIMARY KEY,
            order_id     bigint NOT NULL REFERENCES orders (order_id),
            event_kind   payment_event_kind NOT NULL,
            amount       numeric(18, 2) NOT NULL CHECK (amount > 0),
            occurred_at  timestamptz NOT NULL,
            external_ref text NOT NULL UNIQUE,
            memo         text
        )
        """
    )
    op.execute("CREATE INDEX legacy_payment_events_order_id ON legacy_payment_events (order_id)")


def downgrade() -> None:
    op.execute("DROP TABLE legacy_payment_events")
    op.execute("DROP TABLE order_items")
    op.execute("DROP TABLE orders")
    op.execute("DROP TABLE customers")
    op.execute("DROP TYPE payment_event_kind")
