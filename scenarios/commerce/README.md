# Commerce scenario

DataProof's trusted material for the **Commerce Orders & Refunds** profile defined in [SCENARIO_CONTRACT](../../docs/SCENARIO_CONTRACT.md). This directory is not application source. Pull requests under review change the [sample app](../../sample-app/README.md); the only part of this directory a pull request will be able to propose is `bindings/` (ADR-0005), which does not exist yet.

## Baseline fixture

`commerce_scenario.build_fixture()` generates the baseline from a fixed seed in plain Python, and `load_fixture()` copies it into a database at the reference revision. Reference and candidate environments load identical rows.

| Property | Value |
| --- | --- |
| Fixture version | 1 |
| Seed | `20261030` |
| SHA-256 | `b4f47bea876de4049bedf0968fda8c055f327938fc5010c990d75bfe9e1a0530` |
| Rows | 40 customers, 208 orders, 489 order items, 435 payment events (160 refunds) |

`fixture_violations()` checks every fixture rule in the scenario contract. A unit test requires the published fixture to have none and to match the digest above, and an integration test re-checks key rules in PostgreSQL after loading. Changing what the generator produces is a new fixture version: bump `FIXTURE_VERSION` and republish `PUBLISHED_DIGEST`.

Hand-built orders guarantee the edge cases:

| Order | Edge case |
| --- | --- |
| 1 | Exactly two items, a 120.00 capture and a 49.99 refund (event 1002) |
| 2 | Refunds of 12.50 and 37.49 at the same instant, with `external_ref` order the reverse of `event_id` order |
| 3 | America/Toronto spring-forward: placed at 01:30 EST, captured at 03:30 EDT; no refund |
| 4 | America/Toronto fall-back: capture and refund both at 01:45 local time, an hour apart |
| 5 | Europe/Paris spring-forward, in EUR |
| 6 | 9,876,543,210.99 split into two captures, with a 0.99 refund |
| 7 | Identical 10.01 amounts on one order |
| 8 | Empty-string memo and note alongside NULLs |

Orders 101 to 300 are seeded random orders: one to four items, one or two captures that add up to the item total, zero to two refunds that never exceed the captures, two currencies, and a mix of NULL, empty and filled memos.

## Change sets

`changes/<id>/` holds Alembic revisions exactly as a pull request would add them to `sample-app/migrations/versions/`. Following the benchmark's leakage rules, a change set's ID, file name and docstring never reveal whether it is expected to pass; expected outcomes live in the tests and the design documents.

| Change set | Revision | What the revision says it does |
| --- | --- | --- |
| `ledger-backfill-01` | `5c1e9a7b3d20` | Creates `ledger_entries` and backfills it from `legacy_payment_events` |
| `ledger-backfill-07` | `9f04b2c6e871` | The same, restricted to orders that have items |
