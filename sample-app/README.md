# Commerce sample app

The **Commerce Orders & Refunds** application that DataProof reviews (see "One supported demonstration profile" in [PRODUCT](../docs/PRODUCT.md)). DataProof treats it as untrusted application code. It has its own `pyproject.toml` and `uv.lock`, separate from DataProof's workspace, because ADR-0004 builds the runtime image from this lockfile and refuses to execute pull requests that change it.

It contains Alembic migrations and a FastAPI service. Revision `0001` creates the reference schema: `customers`, `orders`, `order_items` and `legacy_payment_events`, with the `payment_event_kind` enum. The Next.js frontend arrives with the rest of the preview work package.

## API

`app/` is a small read-only FastAPI service, served by `uvicorn app.main:app` from this directory with `DATABASE_URL` set:

| Route | Returns |
| --- | --- |
| `GET /health` | `{"status": "ok"}` |
| `GET /orders?limit=&after=` | Orders by ID, a page at a time |
| `GET /orders/{id}` | The order, its line items, and its captured and refunded totals |
| `GET /orders/{id}/refunds` | Refund events, ordered by instant and then event ID |

Money is exact (`Decimal`, serialized as a string such as `"49.99"`) and instants carry their UTC offset. Each request uses one read-only database session. Payments are read in `app/payments.py`, the module a ledger change replaces (see the [commerce scenario](../scenarios/commerce/README.md#ledger-version-of-the-api)).

## Running the migrations

```sh
cd sample-app
DATABASE_URL=postgresql+psycopg://USER@HOST:PORT/DATABASE uv run alembic upgrade head
```

Revisions are plain SQL. Each one runs in its own transaction, so `alembic_version` records only revisions that committed.

## Running the API

```sh
cd sample-app
DATABASE_URL=postgresql+psycopg://USER@HOST:PORT/DATABASE uv run uvicorn app.main:app
```
