"""Move legacy payment events into a per-order ledger, staging amounts for normalization.

Revision ID: 3b7d1e5a9c24
Revises: 0001
"""

from alembic import op

revision = "3b7d1e5a9c24"
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
        CREATE TABLE payment_amount_staging AS
        SELECT order_id, event_kind, occurred_at, external_ref, amount
        FROM legacy_payment_events
        """
    )
    op.execute(
        """
        WITH events AS (
          SELECT e.event_id, e.order_id, e.event_kind, e.occurred_at,
                 ROW_NUMBER() OVER (PARTITION BY e.order_id, e.event_kind, e.occurred_at
                                    ORDER BY e.event_id) AS tie_rank
          FROM legacy_payment_events AS e
        ), staged AS (
          SELECT s.order_id, s.event_kind, s.occurred_at, s.amount,
                 ROW_NUMBER() OVER (PARTITION BY s.order_id, s.event_kind, s.occurred_at
                                    ORDER BY s.external_ref) AS tie_rank
          FROM payment_amount_staging AS s
        )
        INSERT INTO ledger_entries
          (order_id, ledger_sequence, source_event_id, entry_kind, amount, occurred_at)
        SELECT ev.order_id,
               ROW_NUMBER() OVER (PARTITION BY ev.order_id ORDER BY ev.occurred_at, ev.event_id),
               ev.event_id, ev.event_kind, st.amount, ev.occurred_at
        FROM events AS ev
        JOIN staged AS st
          ON  st.order_id = ev.order_id
          AND st.event_kind = ev.event_kind
          AND st.occurred_at = ev.occurred_at
          AND st.tie_rank = ev.tie_rank
        """
    )
    op.execute("DROP TABLE payment_amount_staging")


def downgrade() -> None:
    op.execute("DROP TABLE ledger_entries")
