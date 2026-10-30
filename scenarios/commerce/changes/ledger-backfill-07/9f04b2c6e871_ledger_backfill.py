"""Move legacy payment events into a per-order ledger, for orders that have items.

Revision ID: 9f04b2c6e871
Revises: 0001
"""

from alembic import op

revision = "9f04b2c6e871"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ledger_entries (
            order_id        bigint NOT NULL REFERENCES orders (order_id),
            ledger_sequence integer NOT NULL CHECK (ledger_sequence > 0),
            source_event_id bigint NOT NULL UNIQUE,
            entry_kind      payment_event_kind NOT NULL,
            amount          numeric(18, 2) NOT NULL CHECK (amount > 0),
            occurred_at     timestamptz NOT NULL,
            PRIMARY KEY (order_id, ledger_sequence)
        )
        """
    )
    op.execute(
        """
        INSERT INTO ledger_entries
          (order_id, ledger_sequence, source_event_id, entry_kind, amount, occurred_at)
        SELECT e.order_id,
               ROW_NUMBER() OVER (PARTITION BY e.order_id ORDER BY e.occurred_at, e.event_id),
               e.event_id, e.event_kind, SUM(e.amount), e.occurred_at
        FROM legacy_payment_events AS e
        JOIN order_items AS oi ON oi.order_id = e.order_id
        GROUP BY e.event_id, e.order_id, e.event_kind, e.occurred_at
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ledger_entries")
