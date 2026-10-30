# Commerce sample app

The **Commerce Orders & Refunds** application that DataProof reviews (see "One supported demonstration profile" in [PRODUCT](../docs/PRODUCT.md)). DataProof treats it as untrusted application code. It has its own `pyproject.toml` and `uv.lock`, separate from DataProof's workspace, because ADR-0004 builds the runtime image from this lockfile and refuses to execute pull requests that change it.

So far it contains Alembic migrations only. Revision `0001` creates the reference schema: `customers`, `orders`, `order_items` and `legacy_payment_events`, with the `payment_event_kind` enum. The FastAPI backend and Next.js frontend arrive with the preview work package.

## Running the migrations

```sh
cd sample-app
DATABASE_URL=postgresql+psycopg://USER@HOST:PORT/DATABASE uv run alembic upgrade head
```

Revisions are plain SQL. Each one runs in its own transaction, so `alembic_version` records only revisions that committed.
