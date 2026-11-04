"""Import invoices from the legacy system's table, guarded by external reference.

Sourced pattern: an import or backfill that is meant to run once but has no effective guard, so
a second run (a retry, a second instance starting at the same time) inserts its rows again.
Here the guard compares ``external_ref`` with ``=``, which never matches a NULL reference, so
every invoice without a reference is imported again with a new ID on each run; the revision
runs the import twice, as two instances would.

Revision ID: 8e3f1b7a4c52
Revises: 0001
"""

from alembic import op

revision = "8e3f1b7a4c52"
down_revision = "0001"
branch_labels = None
depends_on = None

IMPORT = """
INSERT INTO invoices
    (invoice_id, subscription_id, period_start, period_end, subtotal_cents, tax_cents,
     total_cents, issued_at, paid_at, external_ref)
SELECT 100000 * {attempt} + l.invoice_id, l.subscription_id, l.period_start, l.period_end,
       l.subtotal_cents, l.tax_cents, l.total_cents, l.issued_at, l.paid_at, l.external_ref
FROM legacy_invoices AS l
WHERE NOT EXISTS (SELECT 1 FROM invoices AS i WHERE i.external_ref = l.external_ref)
"""


def upgrade() -> None:
    op.execute("CREATE TABLE legacy_invoices (LIKE invoices)")
    op.execute("INSERT INTO legacy_invoices SELECT * FROM invoices")
    for attempt in (1, 2):
        op.execute(IMPORT.format(attempt=attempt))
    op.execute("DROP TABLE legacy_invoices")


def downgrade() -> None:
    op.execute("DELETE FROM invoices WHERE invoice_id >= 100000")
