# SCENARIO CONTRACT — Exact Demo and Ground Truth

**Purpose:** remove ambiguity about which SQL transformations and business facts the complete project will test. This is the approved design, **not** implemented database code: each statement becomes a tested fixture or contract as implementation lands.

## Fixed commerce domain

Reference PostgreSQL schema (`reference` app revision):

- `customers(customer_id PRIMARY KEY, ...)`
- `orders(order_id PRIMARY KEY, customer_id FK, currency CHAR(3), status, placed_at TIMESTAMPTZ, ...)`
- `order_items(order_id FK, line_no, unit_price NUMERIC(18,4), quantity INT, PRIMARY KEY(order_id,line_no))`
- `legacy_payment_events(event_id PRIMARY KEY, order_id FK, event_kind ENUM('CAPTURE','REFUND'), amount NUMERIC(18,2), occurred_at TIMESTAMPTZ, external_ref UNIQUE, ...)`

Candidate schema (`candidate` app revision) normalizes payment/refund events into `ledger_entries(order_id, ledger_sequence, source_event_id UNIQUE, entry_kind, amount NUMERIC(18,2), occurred_at TIMESTAMPTZ, PRIMARY KEY(order_id,ledger_sequence))` with application-accessible order summary views/endpoints.

Every `legacy_payment_events` record of an approved kind maps to exactly **one** `ledger_entries` row, preserving `event_id` as `source_event_id`, order ID, kind, amount and timestamp instant. Its `ledger_sequence` is newly assigned and has **no stable before-key**; matching is by `source_event_id`, not row order. Only post-migration API output that is explicitly declared stable must remain semantically equal.

## Domain invariants

- **C1 Event preservation:** For every reference event ID, candidate has exactly one mapped ledger record with the expected fields. No extra target ledger event without an authorized source/mapping.
- **C2 Refund preservation:** For each `(order_id, currency)`, reference refund totals equal candidate totals exactly as `NUMERIC(18,2)`, including zero-refund orders and keys present on only one side. Compute both grouped totals and reconcile with `FULL OUTER JOIN` over the protected expected order/currency universe and `COALESCE(total, 0)` **without masking a missing expected event** (C1 handles event multiplicity). Evaluate the separate rule `refund_total <= capture_total` on the **reference first**; a bad reference fixture is `ERROR: ORACLE_VIOLATED_ON_REFERENCE`, not a fault of the candidate.
- **C3 Captures:** Capture totals preserve exact cents per order and per currency. Never sum unlike currencies as if equivalent.
- **C4 Relationships:** All target order IDs exist; all customer/order ownership remains intact; no cross-customer or cross-tenant joins.
- **C5 Temporal semantics:** Each event preserves an exact instant in UTC even when display timezone differs; precision/tolerance policy explicit.
- **C6 API behavior:** Reference `GET /orders/{id}` and `GET /orders/{id}/refunds` map to candidate `GET /orders/{id}` and `GET /orders/{id}/transactions?kind=REFUND` through a **reviewed candidate binding**. Match refund events by stable `event_id` ↔ `source_event_id`; compare numeric amount, currency and stable aggregate fields. Sorting and newly introduced non-semantic fields may differ only under explicit approved normalization. API mapping and field selectors are recorded with binding digest.
- **C7 Reprocessing:** Use **Alembic** revisions; the runner consults `alembic_version` to prevent reapplying an already successful revision. Retry tests prove there is no duplicate ledger effect after a completed revision; interrupted/non-transactional revision semantics are reported explicitly rather than assumed idempotent. Source digest records each Alembic revision file.
- **C8 Schema:** Candidate ledger PK/FK/uniqueness and required `NUMERIC` scale exist; intentionally retired legacy fields aren't flagged merely for disappearing.

For any missing comparator input, fail closed as `ERROR`/`NOT_RUN`, not `PASS`.

## Safe and faulty PRs

**Safe PR — `schema/normalize-payment-events`:** New ledger representation and API code. Backfill grouped by **unique `legacy_payment_events.event_id`** and preserves expected mapping; both before/after application previews work. `C1..C8` pass on supported configured fixture. Intentional row/table structural changes are allowed.

**Canonical faulty case F1 — `change/ledger-backfill-07` (neutral name):** A bad `JOIN` multiplies monetary amounts during an otherwise successful grouped backfill. The target retains **one ledger row per source event**, and `source_event_id UNIQUE`, PK/FK, row counts, key sets and null profiles all pass. F1 is caught by C1 exact per-event field comparison and C2 refund total. **An aggregate SUM baseline on the `amount` field also catches F1 when configured**: do not claim this fault outperforms a baseline that would correctly flag it.

```sql
-- F1: sample of the faulty SELECT in the Alembic backfill revision
INSERT INTO ledger_entries
  (order_id, ledger_sequence, source_event_id, entry_kind, amount, occurred_at)
SELECT e.order_id,
       ROW_NUMBER() OVER (PARTITION BY e.order_id ORDER BY e.occurred_at, e.event_id),
       e.event_id, e.event_kind, SUM(e.amount), e.occurred_at
FROM legacy_payment_events AS e
JOIN order_items AS oi ON oi.order_id = e.order_id
GROUP BY e.event_id, e.order_id, e.event_kind, e.occurred_at;
```

For a source REFUND event of $49.99 with two `order_items`, the candidate stores **one $99.98 refund event**, preserving the number of event rows but not its value. Verify that PostgreSQL executes this successfully under real constraints, and that C1/C2 reject it. Note: the query is a test target, not asserted as successfully executed until integration tests exist.

**Secondary F2 — duplicate-row variant:** Use the same join **without** `GROUP BY`, with an intentionally missing `source_event_id UNIQUE` in that specific faulty migration, then demonstrate C1 multiplicity and C8 schema failures after successful execution. F2 is not the hero fault.

**Additional F3 — aggregate-preserving value corruption:** Transpose two unequal refund amounts between **stable event IDs on the same order and in the same currency**, preserving per-order/per-currency refund totals, the same row/key counts and null profiles. A correctly configured total-aggregate B1 baseline can pass this case; C1 keyed value comparison must fail. This is the honest test of deeper semantic verification against aggregate-only reconciliation.

The independent protected `oracle` calculates expected event fields and per-order/per-currency refund totals **only from reference data**. Fault labels are stored in a private benchmark manifest, never in investigator-visible filenames, branch names or commit messages.

**Decimal-loss fixture:** explicitly cast event amount via `::NUMERIC(18,0)` (rounding away cents) or `::integer` (truncation under PostgreSQL cast semantics must be tested), then back to `NUMERIC(18,2)`; include 49.99 and other values with fractional cents to guarantee detectable loss. Do not rely on a `::real` cast of 49.99, which can round-trip back to the same cents. Exact postcondition is a deterministic `Decimal` comparison. Timestamp fault casts `TIMESTAMPTZ` via `::timestamp` then `AT TIME ZONE` with a deliberately mismatched timezone, compared as UTC instants/epoch values.

**Clean intentional-change PR — `change/api-field-expansion`:** Adds a new optional API field and new ledger metadata without changing stable business fields; should pass behavior normalization without false positives.

## Fixture-generation rules

Use deterministic seeded Python/SQL factories (not generated executable SQL from an LLM): at minimum include multiple items per order, multiple captures/refunds, zero-refund orders, two currencies, orders at DST boundaries, large-valued fractional amounts, repeated `external_ref` prefixes across orders, same-cent amounts and NULL optional fields (not nonsensical "composite ledger key collisions"). Publish fixture digest and full seed pseudorandom seed. All seed rows use fictional identities and account identifiers.

## Contract trust and PR changes

**Critical:** Protected base-branch `oracle` fixes expected values, expected key universe, tolerance, requiredness and business semantics. A schema-changing PR can propose only a `candidate_binding` for candidate schema/route/field selectors. Any new or changed binding triggers `AWAITING_BINDING_REVIEW`; the GitHub check is `action_required`/`INCOMPLETE` until a human records a digest-bound `APPROVE_BINDING`. Then DataProof starts a new immutable verification attempt. The binding cannot narrow the protected oracle key universe or alter expected outcomes. See ADR-0005.

## Real-world limitation

This sample tests a focused application profile, not every possible PostgreSQL migration. Larger scale tiers test correctness/performance only at measured fixture sizes. No production-like concurrency guarantee is inferred from a disposable snapshot.
