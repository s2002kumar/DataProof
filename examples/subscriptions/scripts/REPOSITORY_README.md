# Subscriptions: an example repository for the DataProof Action

A small PostgreSQL/Alembic application (plans, customers, subscriptions, invoices) whose pull requests are verified by the [DataProof Action](https://github.com/s2002kumar/DataProof/blob/main/docs/ACTION.md). Every result in this repository's checks tab is a genuine run of the Action on GitHub Actions.

## What to look at

- **The correct migration** replaces integer-cents columns with `numeric(12, 2)` money columns (`subtotal_cents::numeric / 100`). Its pull request proposes a *binding* that tells DataProof how to read the new columns. The first run is `INCOMPLETE`, and the job fails, because a binding needs a maintainer's approval on `main`; after the approval is merged and the job is re-run, it is `CHECKED`.
- **The corrupt migration** is the same change with integer division (`subtotal_cents / 100`). PostgreSQL executes it without error, every constraint holds, and an invoice of 19.99 is now 19.00. With the same approved binding, DataProof reports `BLOCKED`, with the exact expected and observed amounts for each invoice in the job summary.

## How it is wired

| Path | What it is |
| --- | --- |
| `.github/workflows/dataproof.yml` | Runs the Action on every pull request, pinned to a commit, with `permissions: contents: read`. |
| `dataproof/config.toml` | The Action's configuration. Read from `main`, never from the pull request. |
| `dataproof/fixture.sql` | 187 invoices across 45 subscriptions, generated from a fixed seed. Fictional. |
| `dataproof/oracles/` | Three protected contracts: invoice amounts to the cent, billing-period instants, subscription state. |
| `.github/CODEOWNERS` | The workflow and the DataProof directory need an owner's review: a pull request cannot change what it is held to. |
| `alembic.ini`, `migrations/` | The application's migrations. `env.py` reads `DATABASE_URL`. |

The migration runs inside a container with no credentials, no network but its own throwaway PostgreSQL, no Docker socket and no runner filesystem; what that guarantees and what it does not is in [ADR-0007](https://github.com/s2002kumar/DataProof/blob/main/docs/adr/0007-github-action-database-tier.md).

DataProof's full review (previews of both commits, API checks, AI investigation, interactive approval) is the GitHub App, not this Action.
