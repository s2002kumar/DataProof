# Commerce sample app

The **Commerce Orders & Refunds** application that DataProof reviews (see "One supported demonstration profile" in [PRODUCT](../docs/PRODUCT.md)). DataProof treats it as untrusted application code. It has its own `pyproject.toml` and `uv.lock`, separate from DataProof's workspace, because ADR-0004 builds the runtime image from this lockfile and refuses to execute pull requests that change it.

It contains Alembic migrations, a FastAPI service and a Next.js preview frontend. Revision `0001` creates the reference schema: `customers`, `orders`, `order_items` and `legacy_payment_events`, with the `payment_event_kind` enum.

## API

`app/` is a small read-only FastAPI service, served by `uvicorn app.main:app` from this directory with `DATABASE_URL` set:

| Route | Returns |
| --- | --- |
| `GET /health` | `{"status": "ok"}` |
| `GET /orders?limit=&after=` | Orders by ID, a page at a time |
| `GET /orders/{id}` | The order, its line items, and its captured and refunded totals |
| `GET /orders/{id}/refunds` | Refund events, ordered by instant and then event ID |

Money is exact (`Decimal`, serialized as a string such as `"49.99"`) and instants carry their UTC offset. Each request uses one read-only database session. Payments are read in `app/payments.py`, the module a ledger change replaces (see the [commerce scenario](../scenarios/commerce/README.md#ledger-version-of-the-api)).

## Frontend

`web/` is a Next.js 16 app (React 19, TypeScript 5.9) with its own `package.json` and `pnpm-lock.yaml`. It shows the order list and each order's items, totals and refunds, read from the API at `API_URL`. Refunds come from `web/lib/payments.ts`, the module a ledger change replaces.

It is built offline inside the candidate's isolated step (ADR-0004), so it must never fetch anything at build time:

- no `next/font/google`, and no remote image optimization (`images.unoptimized`);
- every page is rendered on request (`dynamic = "force-dynamic"`), so static generation never calls the API;
- telemetry is disabled.

The runtime image installs the dependencies from the lockfile. A step copies the verified source next to them and runs `next build`; a source that ships its own `node_modules` is refused.

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

## Running the frontend

```sh
cd sample-app/web
pnpm install --ignore-workspace
API_URL=http://127.0.0.1:8000 pnpm run build && pnpm run start
```
