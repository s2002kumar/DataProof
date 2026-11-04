"""Introduce a tax-rate table and recompute invoice tax from it, through a join that fans out.

Sourced pattern: an update or insert computed through a join that matches more than one row
per target row. PostgreSQL documents that ``UPDATE ... FROM`` must join at most one row per
row to be modified, or the row used is unpredictable; the deterministic form here sums over
the join instead, so the two rate rows for one region are both counted and tax doubles.

Revision ID: 5e1c7a9b2f60
Revises: 0001
"""

from alembic import op

revision = "5e1c7a9b2f60"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tax_rates (
            region     text NOT NULL,
            rate_bp    integer NOT NULL CHECK (rate_bp BETWEEN 0 AND 10000),
            valid_from date NOT NULL,
            PRIMARY KEY (region, valid_from)
        )
        """
    )
    op.execute(
        """
        INSERT INTO tax_rates (region, rate_bp, valid_from)
        VALUES ('DEFAULT', 1300, '2020-01-01'), ('DEFAULT', 1300, '2026-01-01')
        """
    )
    op.execute("CREATE TABLE invoices_v2 (LIKE invoices INCLUDING ALL)")
    op.execute(
        """
        INSERT INTO invoices_v2
            (invoice_id, subscription_id, period_start, period_end, subtotal_cents, tax_cents,
             total_cents, issued_at, paid_at, external_ref)
        SELECT i.invoice_id, i.subscription_id, i.period_start, i.period_end, i.subtotal_cents,
               SUM(i.subtotal_cents::bigint * r.rate_bp / 10000)::integer,
               i.subtotal_cents + SUM(i.subtotal_cents::bigint * r.rate_bp / 10000)::integer,
               i.issued_at, i.paid_at, i.external_ref
        FROM invoices AS i
        JOIN tax_rates AS r ON r.region = 'DEFAULT'
        GROUP BY i.invoice_id, i.subscription_id, i.period_start, i.period_end,
                 i.subtotal_cents, i.issued_at, i.paid_at, i.external_ref
        """
    )
    op.execute("DROP TABLE invoices")
    op.execute("ALTER TABLE invoices_v2 RENAME TO invoices")
    op.execute(
        "ALTER TABLE invoices ADD FOREIGN KEY (subscription_id) "
        "REFERENCES subscriptions (subscription_id)"
    )


def downgrade() -> None:
    raise NotImplementedError("the original tax amounts are not recoverable")
