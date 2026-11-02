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
- `build-web --dir <frontend>` copies the verified frontend source next to the dependencies baked into the image and runs `next build` offline (Turbopack compiles nothing outside its project root). A source that ships its own `node_modules` is refused. Frontend builds run with `FRONTEND_BUILD_LIMITS` (2 GB, 2 CPUs).
- A digest mismatch exits 65 (`SOURCE_DIGEST_MISMATCH`), and a missing or unsafe archive exits 66 (`SOURCE_INVALID`). In both cases no pull-request code runs.

### Source broker

`GitFetcher` (`broker.py`) fills the trusted mirror that `FETCHING` archives from:

- It fetches the pinned base and head commits **by SHA**, so a branch moved after the event cannot substitute other code. Fetched commits are pinned under `refs/dataproof/commits/` so garbage collection keeps them.
- A credential (for GitHub, an installation token with `contents: read`) reaches git only through the environment of that one command, as an `http.extraHeader`. It is never written to the mirror's configuration, put on a command line, or included in an error. Credential helpers are disabled, inherited `GIT_*` variables are dropped, and prompts are off.
- A failed or timed-out fetch raises, so the worker retries the stage within its budget.

Steps never see the mirror or the credential, only the digest-verified archive.

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
- the frontend building offline from the base and from a candidate change (`tests/integration/test_frontend_build.py`), and failing when it needs the network (`next/font/google`), has a type error or ships its own `node_modules`;
- a preview serving its API and frontend only behind its token, stamped with its commit, and reporting a build failure or a server that cannot start (`tests/integration/test_previews.py`);
- idempotent destroy, and expiry reaping that touches only expired runs in its own scope.

## Pipeline

`Pipeline` supplies the stage handlers for one configured project. `ConfiguredPipelines` serves runs under many: it loads each run's configuration from the control database by the digest the run pins, checks that it hashes to that digest, and hands each stage to a `Pipeline` built for that configuration alone. A run whose configuration is missing or altered ends `ERROR: CONFIG_DIGEST_MISMATCH` and is still cleaned up.

Each stage records its results through the transition's `effects`, in the control database's write-once result tables, so results exist exactly when the run has moved past the stage that produced them.

| Stage | What it does | Ends in |
| --- | --- | --- |
| `FETCHING` | Checks that the trusted `ProjectConfig` still has the digest the run was pinned to, has the source broker fetch the exact base and head commits into the trusted mirror if they are missing, then stages exact base and head archives in the artifact store | `ANALYZING`, or `ERROR: CONFIG_DIGEST_MISMATCH` |
| `ANALYZING` | Refuses other repositories, a fork's pull request (`FORK_PULL_REQUEST`), and changes outside the allowed roots or to any dependency or runtime file (`pyproject.toml`, lockfiles, `package.json`, `Dockerfile` and similar, anywhere in the tree), before anything is provisioned | `PROVISIONING`, or `NOT_EXECUTED` with a neutral report |
| `PROVISIONING` | Creates the reference and candidate environments, migrates both to the base schema **with the base source** inside steps, loads the baseline, proves both databases identical by a digest of every row, and serves the reference preview from the base source | `MIGRATING`, or `ERROR: DATASET_DIGEST_MISMATCH`, `BASE_MIGRATION_FAILED`, `SOURCE_DIGEST_MISMATCH`, `BASELINE_DIVERGED` or `REFERENCE_PREVIEW_FAILED` |
| `MIGRATING` | Runs the head's migrations in the candidate, from the head source, inside a step, then builds and serves the candidate preview from the head source. A retried attempt first rebuilds the candidate, so the recorded migration is one clean execution | `VERIFYING` |
| `VERIFYING` | The required `migration.execute` check (`FAIL` if the migration errored or overran) and `preview.candidate` check (`ERROR: CANDIDATE_BUILD_FAILED` or `CANDIDATE_START_FAILED`), then every oracle **from the base archive** against the binding the pull request proposes **in the head archive**: data contracts through the read-only role, and API behaviour contracts (C6) through both previews' edges, which they need to be serving (otherwise `NOT_RUN: PREREQUISITE_FAILED`). If the migration failed, the contracts are `NOT_RUN: PREREQUISITE_FAILED`. A missing binding is `NOT_RUN: AWAITING_BINDING_REVIEW` | `INVESTIGATING` if a required check failed or errored, else `REPORTING` |
| `INVESTIGATING` | The model-off diagnosis, and, when a model is configured, a bounded investigation of the live environments through the read-only role and the head commit's allowlisted source (`packages/investigator`). Both are recorded beside the checks and reported as advisory; neither can change a check or the verdict | `REPORTING` |
| `REPORTING` | Seals evidence, then derives the verdict and its GitHub conclusion with the coverage table (see below) | `READY_FOR_REVIEW` |
| cleanup | Destroys every environment of the run | `COMPLETE` or `SUPERSEDED` |

### Previews

Each environment serves its application from exactly its commit's source, for review:

| Container | Role |
| --- | --- |
| `<prefix>-app` | The `serve` step: builds the frontend offline from the verified source, then runs the API (uvicorn) and the frontend (`next start`) and supervises them. It reads the database with the read-only role, has no published ports, and runs with `FRONTEND_BUILD_LIMITS`. A build failure exits 67 and a server that dies exits 68. |
| `<prefix>-edge` | A trusted, digest-pinned nginx (unprivileged image, all capabilities dropped), the only preview component published on `127.0.0.1`. Every route except `/__dataproof/health` answers 401 without the run-scoped preview token. `/__dataproof/login?token=...` sets the token as an HTTP-only, `SameSite=Strict` cookie. The edge proxies `/api/` to the API and everything else to the frontend, forwards only to its own app container (never to an address a client names), drops the cookie before proxying, and stamps every response with `X-DataProof-Environment` and `X-DataProof-Commit`. |

`run_previews` records each preview's commit, URL, readiness or failure, build and ready time, peak memory and CPU seconds (ADR-0004's measured gate, #5). It stores the token's SHA-256, never the token; the trusted operator recovers access with `preview_access()`. Previews stay up while the run is `READY_FOR_REVIEW`, and cleanup removes them with everything else. Remote reviewers reach previews through the signed gateway in [`services/api`](../api/README.md#signed-preview-gateway).

### Sealed evidence

Before a run is reported, its evidence is sealed so that diagnosis and reproduction never depend on an environment that cleanup destroys (ARCHITECTURE, evidence store):

1. **Snapshot.** Trusted code takes `pg_dump --format=custom` of each environment, inside the trusted database container, as the read-only role. The dump is written once to the artifact store with its SHA-256.
2. **Restore verification.** Each dump is restored into a fresh environment of the same role (variant `restore`). The verification records a digest of every row of every application table in the live and the restored database. The restore environment is then destroyed; a failed restore is recorded, not hidden.
3. **Manifest.** An `EvidenceManifest` lists the input, dataset, config and binding digests, both image digests, the source archives, the snapshots and their restore verification, and a results bundle (checks, evidence and step output), each with its SHA-256. It is stored as `manifest.json`. The report cites the manifest's digest.

Snapshots and the manifest are recorded in the control database's write-once tables in the same transaction as the report.

**Replay.** `Pipeline.replay(run)` re-runs a sealed run's data contracts from its evidence alone, even after cleanup:

1. It re-verifies the manifest and both snapshots by SHA-256, and refuses to replay under a configuration other than the run's own.
2. It restores the snapshots into fresh `replay` environments and evaluates every data contract exactly as `VERIFYING` does.
3. It destroys those environments and compares outcomes, mismatches and evidence digests with what the run recorded.

API behaviour checks need live previews, so a replay reports them as skipped, not as matched. A replay never changes the run. Diagnosis and reproduction start from this.

### Reproduction

`Pipeline.reproduce(run, check_id)` replays one failed data contract on a small subset of the sealed baseline:

1. **Seed keys.** It takes the check's first three failing keys, in a fixed order, with the columns that differed.
2. **Subset.** It restores the sealed reference snapshot into a fresh environment and extracts an FK-closure subset by a fixed rule (`subset.py`):
   - the seed rows, in the table that holds the oracle's keys;
   - the rows that reference them or their direct parents (their aggregate, such as an order's items and other events);
   - every row those reference.
3. **Replay.** It loads the subset into fresh reference and candidate environments, migrates both to the base schema, runs the head migration from the head source in the candidate, and evaluates the contract again.
4. **Verdict.** `REPRODUCED` needs every seed key to fail again on the same columns. These rules live in `signature.py`, so the benchmark applies exactly the same ones. Otherwise the result is `NOT_REPRODUCED` with the reason. The subset is never widened until a failure recurs (#8).

The subset is stored as an artifact with its digest, the `Reproduction` is recorded write-once, and every environment used is destroyed. The run is never changed.

Reviewers request reproductions through the API. `Reproducer` (`reproducer.py`) runs the queued requests, each under its run's stored configuration (`ConfiguredPipelines`). A request that cannot succeed ends at once with the reason: the check did not fail, the run has no sealed evidence, or the check is an API contract. Any other failure is retried until the job's attempts are spent.

### Binding decisions

Each check records the single binding it was evaluated against or is waiting on. `decisions.decide_binding(store, results, decision)` checks a reviewer's decision against the run: it must name a binding that one of the run's checks is waiting on. An approval then starts a new run under the run's own configuration plus every binding decision made at its head (`RunStore.decide_binding`). Who may decide is enforced by the reviewer API (#47), which does not exist yet.

Problems caused by the pull request become check outcomes; problems with the trusted setup become a typed run `ERROR`; transient failures are retried. Source archives are written once to `LocalArtifactStore` and re-verified by SHA-256 on every read.

`tests/integration/test_pipeline.py` runs commerce change sets end to end on local Docker:

- **Safe change:** `CHECKED` (success), even after a failed verification attempt that the next attempt recovers from by reattaching to both environments.
- **F1:** `BLOCKED` with the 49.99 to 99.98 refund mismatch. Run twice, it gives identical outcomes and evidence digests.
- **Unapproved bindings:** `INCOMPLETE` (action required), with the binding analysis shown.
- **Lockfile change:** `NOT_EXECUTED` (neutral), with nothing provisioned.
- **Fork:** a pull request from a fork is `NOT_EXECUTED: FORK_PULL_REQUEST` (neutral), with nothing provisioned.
- **Source broker:** a run whose commits exist only in the remote fetches them into an empty mirror. The staged archives have exactly the digests of archives made from the remote, and `ANALYZING` reads the changed paths from the mirror.
- **Failing migration:** `BLOCKED`, with the contracts not run.
- **Cancellation during migration:** the late result is discarded and nothing is left behind.
- **Configuration drift:** an error, and nothing runs.
- **Weakened oracle:** a pull request that makes its own oracle optional is still `BLOCKED`.
- **Sealed evidence:** both snapshots restore to identical rows. The manifest matches its stored object, and every artifact it lists is present and unaltered. After F1's environments are destroyed, restoring its sealed candidate snapshot still shows the 99.98 refund, a replay from the sealed snapshots reproduces C1–C3 exactly, and a snapshot tampered with on disk is refused.
- **Supersession:** a new head on the same pull request cancels a run in review. That run is cleaned up and ends `SUPERSEDED`.
- **Previews:** a run serves the reference at the base SHA and the candidate at the head SHA. Through each edge with its token, the reference API's refunds and the candidate API's ledger transactions both show the 49.99 refund, and so does each frontend page. Without the token, every route answers 401. Through the signed gateway with a grant, the candidate preview serves the same API and page. Cleanup removes both previews. A candidate frontend that does not build gives `ERROR: CANDIDATE_BUILD_FAILED` (`BLOCKED`); the data checks still run, and the API checks are `NOT_RUN`.
- **Investigation:** an F1 run with a scripted model goes through `INVESTIGATING`; the model reads the live candidate (refund 1002 at 99.98), and both its investigation and the model-off diagnosis are reported as advisory while the verdict stays `BLOCKED`. A hostile model's `DELETE` and `approve_binding` are denied, its "every check passes" answer is rejected, and the data and verdict are unchanged.
- **Reproduction:** after cleanup, a queued request for F1's C1, run by `Reproducer` under the stored configuration, reproduces on a subset of under 100 of the baseline's rows (customers, orders, items, events). A fault that corrupts data only when the events table holds over 100 rows is honestly `NOT_REPRODUCED` ("the check is PASS on the subset"). Only a failed check can be reproduced.
- **Correction replay:** after F1 is `BLOCKED`, a correction commit that replaces the faulty revision with the safe one runs as a new child run under its own configuration and is `CHECKED`. The original run's report and checks are unchanged, and it is not superseded.
- **Binding approval:** a safe change with unreviewed bindings is `INCOMPLETE`, and each of C1–C3 records the binding it waits on. A decision on any other binding is refused. Approving the three bindings starts runs under new configurations, and the one that carries all three is `CHECKED` with C1–C3 passing. The original stays `INCOMPLETE` and is not superseded.
- **Unknown configuration:** a run pinned to a configuration that is not stored ends `ERROR: CONFIG_DIGEST_MISMATCH`, with nothing staged.
- **API behaviour (C6):** on the safe change both C6 checks pass for all 208 orders through the previews. On F1 the app itself works (`preview.candidate` passes), but both C6 checks fail, with refund 1002 at 49.99 expected and 99.98 observed through the candidate API.

## Status

Work package 4 is complete on local Docker. Real pull-request migrations run end to end, from the control store through sealed, restore-verified evidence to cleanup, with retries, cancellation, timeouts and supersession. Work package 5 adds live reference and candidate previews behind token-checking edges, and API behaviour checks (C6) through them. Work packages 6–8 add sealed evidence and replay, the advisory investigator, reproduction and correction replay. Work package 9 has begun with binding decisions, runs under stored configurations, GitHub webhook intake and commit-bound check runs ([`services/api`](../api/README.md)); there is no user interface yet.
