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

## Pipeline

`Pipeline` supplies the stage handlers for one configured project. Each stage records its results through the transition's `effects`, in the control database's write-once result tables, so results exist exactly when the run has moved past the stage that produced them.

| Stage | What it does | Ends in |
| --- | --- | --- |
| `FETCHING` | Checks that the trusted `ProjectConfig` still has the digest the run was pinned to, then stages exact base and head archives in the artifact store | `ANALYZING`, or `ERROR: CONFIG_DIGEST_MISMATCH` |
| `ANALYZING` | Refuses other repositories, and changes outside the allowed roots or to any dependency or runtime file (`pyproject.toml`, lockfiles, `package.json`, `Dockerfile` and similar, anywhere in the tree), before anything is provisioned | `PROVISIONING`, or `NOT_EXECUTED` with a neutral report |
| `PROVISIONING` | Creates the reference and candidate environments, migrates both to the base schema **with the base source** inside steps, loads the baseline, and proves both databases identical by a digest of every row | `MIGRATING`, or `ERROR: DATASET_DIGEST_MISMATCH`, `BASE_MIGRATION_FAILED`, `SOURCE_DIGEST_MISMATCH` or `BASELINE_DIVERGED` |
| `MIGRATING` | Runs the head's migrations in the candidate, from the head source, inside a step. A retried attempt first rebuilds the candidate, so the recorded migration is one clean execution | `VERIFYING` |
| `VERIFYING` | The required `migration.execute` check (`FAIL` if the migration errored or overran), then every oracle **from the base archive** against the binding the pull request proposes **in the head archive**, through the read-only role. If the migration failed, the contracts are `NOT_RUN: PREREQUISITE_FAILED`. A missing binding is `NOT_RUN: AWAITING_BINDING_REVIEW` | `REPORTING` |
| `REPORTING` | Seals evidence, then derives the verdict and its GitHub conclusion with the coverage table (see below) | `READY_FOR_REVIEW` |
| cleanup | Destroys every environment of the run | `COMPLETE` or `SUPERSEDED` |

### Sealed evidence

Before a run is reported, its evidence is sealed so that diagnosis and reproduction never depend on an environment that cleanup destroys (ARCHITECTURE, evidence store):

1. **Snapshot.** Trusted code takes `pg_dump --format=custom` of each environment, inside the trusted database container, as the read-only role. The dump is written once to the artifact store with its SHA-256.
2. **Restore verification.** Each dump is restored into a fresh environment of the same role (variant `restore`). The verification records a digest of every row of every application table in the live and the restored database. The restore environment is then destroyed; a failed restore is recorded, not hidden.
3. **Manifest.** An `EvidenceManifest` lists the input, dataset, config and binding digests, both image digests, the source archives, the snapshots and their restore verification, and a results bundle (checks, evidence and step output), each with its SHA-256. It is stored as `manifest.json`. The report cites the manifest's digest.

Snapshots and the manifest are recorded in the control database's write-once tables in the same transaction as the report.

Problems caused by the pull request become check outcomes; problems with the trusted setup become a typed run `ERROR`; transient failures are retried. Source archives are written once to `LocalArtifactStore` and re-verified by SHA-256 on every read.

`tests/integration/test_pipeline.py` runs commerce change sets end to end on local Docker:

- **Safe change:** `CHECKED` (success), even after a failed verification attempt that the next attempt recovers from by reattaching to both environments.
- **F1:** `BLOCKED` with the 49.99 to 99.98 refund mismatch. Run twice, it gives identical outcomes and evidence digests.
- **Unapproved bindings:** `INCOMPLETE` (action required), with the binding analysis shown.
- **Lockfile change:** `NOT_EXECUTED` (neutral), with nothing provisioned.
- **Failing migration:** `BLOCKED`, with the contracts not run.
- **Cancellation during migration:** the late result is discarded and nothing is left behind.
- **Configuration drift:** an error, and nothing runs.
- **Weakened oracle:** a pull request that makes its own oracle optional is still `BLOCKED`.
- **Sealed evidence:** both snapshots restore to identical rows. The manifest matches its stored object, and every artifact it lists is present and unaltered. After F1's environments are destroyed, restoring its sealed candidate snapshot still shows the 99.98 refund.
- **Supersession:** a new head on the same pull request cancels a run in review. That run is cleaned up and ends `SUPERSEDED`.

## Status

Work package 4 is complete on local Docker. Real pull-request migrations run end to end, from the control store through sealed, restore-verified evidence to cleanup, with retries, cancellation, timeouts and supersession. Next are full application previews (work package 5). There is no GitHub integration or user interface yet.
