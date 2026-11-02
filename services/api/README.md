# DataProof control plane API

The control plane's HTTP surface and its GitHub connector (ARCHITECTURE, GitHub connector and API surface). It is being built in work package 9 (#47). Implemented so far: GitHub webhook intake, GitHub App authentication, commit-bound check runs and the GitHub remote for the source broker.

## Webhook intake

`POST /github/webhook` turns verified GitHub pull request events into runs.

1. **Signature first.** `X-Hub-Signature-256` must be the HMAC-SHA256 of the exact raw body under the shared webhook secret, compared in constant time. Anything else is `401`, and nothing is recorded. Bodies over 5 MiB are `413`; a signed body that is not a JSON object is `400`.
2. **Recorded once.** Each verified delivery is recorded write-once in `github_deliveries` with what intake did about it. A redelivery is answered from that record (`duplicate: true`).
3. **Trusted only as far as the signature goes.** The repository must be one the operator registered, the event must come from the GitHub App installation registered for it, and the pull request must target that repository. Otherwise the delivery is `IGNORED` or `REFUSED`, and no run is created.
4. **Pinned runs.** `opened`, `reopened`, `synchronize` and `ready_for_review` create a `webhook` run pinned to the event's exact base and head SHAs and to the operator's trusted configuration, which is stored under its digest so a worker can load it. Nothing the pull request says (title, body, branch names) is used. Runs are idempotent on their inputs, so a redelivered or repeated event finds its run (`RUN_EXISTS`). A new head supersedes the runs for older heads.
5. **Forks.** A fork's pull request becomes a run with `head_repository` set, which `ANALYZING` refuses as `NOT_EXECUTED: FORK_PULL_REQUEST` before anything is provisioned. It is reported, not silently dropped.
6. **Closing.** `closed` cancels the pull request's working and reviewable runs, which are then cleaned up.

The pull request's author and head repository are recorded per head in `github_pull_request_heads`; the approver rule (ADR-0005) needs the author.

## GitHub App authentication

`GitHubApp` signs a short-lived RS256 JWT with the App's private key (`iat` backdated a minute, lifetime nine minutes) and exchanges it for an installation token. The token is scoped to **one repository** and to `checks: write`, `contents: read`, `metadata: read` and `pull_requests: read`. Tokens are cached until five minutes before they expire. The key and tokens stay in the trusted control plane: they are never given to a run's containers, and a refused request raises `GitHubError` with the status and route only.

## Check runs

`CheckPublisher` reports each pull request run as one check run named `DataProof`, on the run's **exact head commit**, with the run's ID as its external ID. It publishes from the control database, so a run's status reaches GitHub whatever process changed it.

| Run | Check run |
| --- | --- |
| queued | `queued` |
| working | `in_progress` |
| reported | `completed` with the report's conclusion, its title (failed and not-run required checks) and the coverage table |
| error before reporting | `completed`, `failure` (with the error code) |
| timed out | `completed`, `timed_out` |
| cancelled or superseded | `completed`, `cancelled` |

An interrupted run is never green. `NOT_EXECUTED` is `neutral`, which GitHub branch protection counts as passing; see #51.

Rules:

- **Recorded once, then repeated only if missing.** Each status is recorded write-once (migration 0009) after GitHub accepted it. A status that failed to publish, for example during an outage, is published on the next pass.
- **One check run per run.** A per-run lock keeps concurrent publishers from creating two. Before creating one, the publisher looks for an existing check run with the run's external ID on that commit, so a crash between GitHub accepting a check run and recording it does not duplicate it.
- **Trusted content only.** The summary holds check IDs, outcomes, reasons and counts from the run's results, plus the run's commits and configuration digest. No text from the pull request appears in it.
- **Scope.** Only runs of registered repositories with a pull request are published.

## Source fetching

`github_remotes(app, installations)` tells the runner's source broker where a registered repository lives on GitHub. Each fetch gets a fresh installation token for that repository alone, which the broker passes to git through one command's environment ([runner](../runner/README.md#source-broker)).

## Not built yet

These are the remaining steps of work package 9 (#47):

- the reviewer API with sign-in and authorization;
- the signed preview gateway;
- the dashboard.

No GitHub App is registered for this project. Everything here is tested against signed fixture events and a fake GitHub (`tests/github_env.py`) that verifies App JWTs against the App's public key and checks token scope and expiry. Nothing has talked to the real GitHub.

## Tests

- **`tests/unit/test_webhooks.py`:** signature verification, including altered bodies, wrong secrets, missing or `sha1=` signatures. Event parsing accepts people and apps (`dependabot[bot]`) and rejects malformed logins.
- **`tests/integration/test_github_intake.py`** (PostgreSQL), signed deliveries through the FastAPI app:
  - pinned runs, the stored configuration, the recorded author and the actor on the first transition;
  - redeliveries, and supersession by a new head;
  - forged deliveries, which record nothing;
  - unconfigured repositories, foreign installations and cross-repository pull requests;
  - forks and closing;
  - signed bodies that are not events.
- **`tests/unit/test_github_app.py`:** the token request's repository and permission scope, caching and renewal near expiry, a JWT from another key refused, a repository outside the installation refused, and no token in errors.
- **`tests/unit/test_source_broker.py`** (git only):
  - pinned commits are fetched by SHA, including a commit that is no longer the branch tip, and are kept under `refs/dataproof/`;
  - commits already present need no remote;
  - the credential appears in no file of the mirror;
  - git sees the credential only as that command's header;
  - inherited `GIT_*` settings are dropped;
  - a missing commit is an error that does not carry the credential;
  - only full SHAs are accepted;
  - the GitHub remote uses a token scoped to that repository alone.
- **`tests/integration/test_check_runs.py`** (PostgreSQL):
  - `queued`, then `in_progress`, then `completed: failure` on the head commit, with the coverage table, and nothing re-sent once complete;
  - cancelled, superseded, errored and timed-out runs completed as `cancelled`, `cancelled`, `failure` and `timed_out`;
  - a check run created before a crash is found, not duplicated;
  - an outage is retried;
  - other repositories and runs without a pull request are not published;
  - four concurrent publishers create one check run;
  - `NOT_EXECUTED` is `neutral`.
- **`tests/integration/test_pipeline.py`** (Docker):
  - a fork's run is `NOT_EXECUTED` (neutral) and nothing is provisioned;
  - a signed F1 `pull_request` event goes through intake, a worker under the stored configuration and the publisher, and ends as a `failure` check run on F1's head commit (`queued`, then `completed`) with C1 failing in its table, and nothing on the base commit.
