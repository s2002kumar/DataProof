"""Move legacy payment events into a per-order ledger with wider amount precision.

Revision ID: f5c9e3a7b261
Revises: 0001
"""

from alembic import op

revision = "f5c9e3a7b261"
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
            amount          numeric(19, 4) NOT NULL CHECK (amount > 0),
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
               e.event_id, e.event_kind, e.amount::numeric(19, 4),
               (e.occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        FROM legacy_payment_events AS e
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ledger_entries")
