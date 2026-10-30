# dataproof-runner

The run supervisor's workers. A `Worker` claims a run from the [control store](../../packages/control/README.md) and executes stage handlers in order under a heartbeated, fenced lease. The lifecycle, not the handlers, decides what happens after an interruption.

## Stage contract

A handler receives a `StageContext` (the run and its claim) and returns a `StageOutcome`: the next state, a detail record for the transition log, and optionally an `error_code`, a `not_executed_reason`, an `isolation_id`, or `effects` to commit in the same transaction as the transition.

- Handlers are required for `FETCHING`, `ANALYZING`, `PROVISIONING`, `MIGRATING`, `VERIFYING` and `REPORTING`. `INVESTIGATING` and `REPRODUCING` run only when a handler moves the run there.
- A handler that raises has failed this attempt. The worker releases the run, and the stage is retried in place, with backoff, until the retry budget is spent.
- Long handlers call `context.ensure_lease()` between steps. A heartbeat that finds the lease revoked (cancellation, timeout or supersession) or expired makes it raise, so the handler stops promptly. The store also refuses every write from a worker that lost its lease, so a late result is discarded, never recorded.
- The cleanup handler must be idempotent. It may run after a crash, or for a run that never provisioned anything.

When a run reaches `READY_FOR_REVIEW` the worker parks it until the review TTL expires; the next claim tears its environments down.

## Execution backends

The supervisor reaches environments only through `ExecutionBackend` (`backend.py`, ADR-0006): provision one role of a run, run a bounded step in it, list, destroy and reap expired resources. `LocalDockerBackend` is the first implementation. For each run and role it creates:

| Resource | Isolation |
| --- | --- |
| `<prefix>-int` network | Docker `internal` network: no route to the internet, the host or other runs. Steps attach only here and reach their own database as `postgres`. |
| `<prefix>-ctl` network | Holds only the database, so that its port can be published on `127.0.0.1` for trusted host-side services. |
| `<prefix>-db` container | PostgreSQL 17 by digest, user `postgres`, all capabilities dropped, `no-new-privileges`, memory/CPU/PID limits, data on tmpfs. An init script creates `dataproof_owner` (owns the application database; no superuser, role or database creation) and `dataproof_reader` (`pg_read_all_data`, read-only sessions). The superuser password is generated, given only to this container and never stored. |
| `<prefix>-step-<id>` containers | The trusted runtime image by image ID or digest only, UID 10001, all capabilities dropped, `no-new-privileges`, limits and tmpfs work areas; removed after the step. Only `DATABASE_URL` for the owner role is passed in; step output is redacted of both database passwords and truncated. |

Every resource is labelled with its scope, run, role and expiry. `destroy(run_id)` and `reap_expired(now)` work from labels alone, so cleanup does not depend on the control plane being up.

### Runtime image and source delivery

[`infra/docker/runtime/Dockerfile`](../../infra/docker/runtime/Dockerfile) builds the trusted runtime image from digest-pinned base images and the sample app's lockfile; a step never installs anything. The trusted supervisor stages an exact-commit archive (`source.py`, `git archive` of a trusted clone for now) and copies it into the step container before start. The image's [entrypoint](../../infra/docker/runtime/entrypoint.py), running as the unprivileged user, verifies the archive's SHA-256 against the launch parameter and unpacks it with tar's `data` filter. Only then does it run the step:

- `migrate --config <alembic.ini> --target <revision>` runs Alembic from the pull request's source with a minimal environment.
- A digest mismatch exits 65 (`SOURCE_DIGEST_MISMATCH`), and a missing or unsafe archive exits 66 (`SOURCE_INVALID`). In both cases no pull-request code runs.

### Tested isolation

`tests/integration/test_local_docker.py` provisions real environments. A hostile migration probes its sandbox from inside a candidate step and records what it could reach. Only its own database is reachable, and the following are refused:

- the internet, by address and by name;
- the host gateway and loopback;
- the reference database.

The same probe confirms the step's identity and environment:

- it runs as UID 10001 with no effective capabilities and `NoNewPrivs`;
- it has no Docker socket;
- its environment holds only allowlisted variables;
- the database URL it prints is redacted in the step output.

Other tests cover:

- the owner role being refused `COPY TO PROGRAM`, `pg_read_file`, `CREATE EXTENSION dblink` and role creation, and the reader being refused writes;
- digest mismatches and escaping archives, with no pull-request code run;
- a failing migration committing nothing of itself, and a killed overrunning step;
- tag-only images being refused;
- idempotent destroy, and expiry reaping that touches only expired runs in its own scope.

## Status

The worker, its lifecycle guarantees and the local Docker backend are implemented and tested. The pipeline stages that use them, to fetch and analyze a change, provision both environments, load the identical baseline, migrate, verify, report and clean up, are the next step of work package 4, followed by sealed snapshots (#23).
