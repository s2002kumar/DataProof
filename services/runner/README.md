# dataproof-runner

The run supervisor's workers. A `Worker` claims a run from the [control store](../../packages/control/README.md) and executes stage handlers in order under a heartbeated, fenced lease. The lifecycle, not the handlers, decides what happens after an interruption.

## Stage contract

A handler receives a `StageContext` (the run and its claim) and returns a `StageOutcome`: the next state, a detail record for the transition log, and optionally an `error_code`, a `not_executed_reason`, an `isolation_id`, or `effects` to commit in the same transaction as the transition.

- Handlers are required for `FETCHING`, `ANALYZING`, `PROVISIONING`, `MIGRATING`, `VERIFYING` and `REPORTING`. `INVESTIGATING` and `REPRODUCING` run only when a handler moves the run there.
- A handler that raises has failed this attempt. The worker releases the run, and the stage is retried in place, with backoff, until the retry budget is spent.
- Long handlers call `context.ensure_lease()` between steps. A heartbeat that finds the lease revoked (cancellation, timeout or supersession) or expired makes it raise, so the handler stops promptly. The store also refuses every write from a worker that lost its lease, so a late result is discarded, never recorded.
- The cleanup handler must be idempotent. It may run after a crash, or for a run that never provisioned anything.

When a run reaches `READY_FOR_REVIEW` the worker parks it until the review TTL expires; the next claim tears its environments down.

## Status

The worker and its lifecycle guarantees are implemented and tested against PostgreSQL with stage handlers that only record calls. The real stages, which provision reference and candidate environments on the local Docker backend (ADR-0006), run migrations, verify and seal snapshots, are the next steps of work package 4 (#23).
