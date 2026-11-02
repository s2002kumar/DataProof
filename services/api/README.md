# DataProof control plane API

The control plane's HTTP surface and its GitHub connector (ARCHITECTURE, GitHub connector and API surface). It is being built in work package 9 (#47). Implemented so far: GitHub webhook intake, GitHub App authentication, commit-bound check runs, the GitHub remote for the source broker, the reviewer API (sign-in, reads and decisions) and the signed preview gateway.

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

## Reviewer API

The reviewer API is mounted when `create_app` is given a `Reviewer`.

**Sign-in.**

- `GET /auth/login` starts GitHub's OAuth web flow for the App. A random state is remembered in a signed, ten-minute cookie (`HttpOnly`, `SameSite=Lax`, path `/auth`).
- `GET /auth/callback` accepts only that state. It exchanges the one-time code for the user's token, reads the login from `GET /user`, and discards the token: DataProof never acts as the user.

**Sessions.**

- A session is a signed cookie: the login, a random CSRF token and an expiry, signed with HMAC-SHA256 and compared in constant time. Attributes: `HttpOnly`, `Secure` on HTTPS, `SameSite=Lax`, eight hours. There is no server-side session table.
- `GET /api/session` returns the login, the CSRF token and the repositories the login can read.

**Authorization comes from GitHub, on every request.**

- The login's permission on the run's repository is read with the App's installation token: `read` or better to see runs, and `write` or `admin` to decide on them.
- Answers are cached for 60 seconds, so a collaborator who loses access loses it within a minute, not at the end of the session.
- A run the reviewer cannot read gets exactly the same `404` as a run that does not exist.

**Reads.**

- `GET /api/runs` lists runs of the readable registered repositories, newest first. It can be filtered by repository and pull request.
- `GET /api/runs/{id}` returns the run, its transition log, the pull request's author, the report, decisions, investigations, reproductions, previews (never a token or its hash) and the GitHub check run.
- `GET /api/runs/{id}/checks` returns each check with the binding it waits on or used, that binding's digest (computed here, so clients never re-implement the canonical form) and its analysis.
- `GET /api/runs/{id}/evidence` returns the run's evidence records.

**Writes** need the session, the CSRF token in `X-CSRF-Token`, and an `Origin` equal to the dashboard's. A missing or foreign `Origin` is refused. Deciding also needs `write` permission on the run's repository. Every decision is recorded as a `HumanDecision`, built on the server from the session; none changes a check result.

- `POST /api/runs/{id}/binding-decisions` with `APPROVE_BINDING` or `REJECT_BINDING` and a binding digest (ADR-0005):
  - The decision is bound to the run's head commit and to the pull request's author as GitHub reported it.
  - **Approver rule:** the author may approve only while the repository has no other maintainer (collaborators who can push, read at decision time). Such an approval is recorded as `PROCEDURAL` separation; anyone else's approval is `ORGANIZATIONAL`.
  - The binding must be one a check of the run waits on (`422` otherwise). A head the pull request has left, or a decision contradicting an earlier one, gets `409`. A repeated decision returns the earlier one.
  - An approval starts a `binding_approval` run.
- `POST /api/runs/{id}/cancel` cancels a working or reviewable run and records `CANCEL_RUN` in the same transaction. A run already ending gets `409`.
- `POST /api/runs/{id}/correction-replay` with a commit SHA:
  - The commit must exist in the repository, checked on GitHub, and differ from the run's head.
  - It starts a `correction_replay` run under the repository's registered configuration, recorded as `REQUEST_CORRECTION_REPLAY` in the same transaction. The original run is untouched.
- `POST /api/runs/{id}/reproductions` with a check ID queues a reproduction (`202`).
  - The check must have failed and be a data contract, and the run must have sealed its evidence; otherwise `422` or `409`.
  - Asking again returns the existing request.
  - Reproduction takes minutes in fresh environments, so a worker runs it, not the request. The run's detail lists each request's state: `queued`, `running`, `reproduced` or `failed`, with the reason.
- `POST /auth/logout` clears the session cookie.

## Signed preview gateway

`Gateway` (`gateway.py`) is how remote reviewers reach a run's live previews (THREAT_MODEL; #4). It is a separate ASGI app.

- **A separate origin per preview.** A preview runs the pull request's own frontend code. It is served from its own host, `<role>-<run_id>.<preview domain>`, never from the dashboard's origin, where that code could read the reviewer's CSRF token and act for them. Use a separate registrable domain for previews, with a wildcard certificate.
- **The host selects, it never targets.** The host names a run and a role. The upstream is always the edge that the supervisor registered for that run and role (`backend_resolver`, the backend's `preview_access`). No host, port or URL from the client is ever contacted.
- **Signed, expiring, bound grants.** `POST /api/runs/{id}/previews/{role}` (reviewer API: session, CSRF, `read` permission) returns a link to `/__dataproof/grant?token=…` on the preview host. The grant is HMAC-signed and binds the run, the role and the reviewer, for one hour. The gateway turns it into a host-only, `HttpOnly`, `Secure`, `SameSite=Lax` cookie. Every request re-verifies the grant, checks that it is for this host's run and role, re-checks the reviewer's permission on the repository, and resolves the registered upstream again.
- **A narrow proxy.** `GET` and `HEAD` only; other methods get `405`. Only an allowlist of request headers is forwarded, plus the edge's own run token; cookies, `Authorization` and forwarding headers are dropped. No `Set-Cookie` from a preview reaches the browser. A redirect to the edge becomes a relative one, and a redirect elsewhere is dropped.
- **Errors.** An unknown host is `404`; a missing, expired or tampered grant `401`; a grant for another run or role, or lost permission, `403`; a preview that is not running `503`.

## OpenAPI

`services/api/openapi.json` is the API's OpenAPI document, which the dashboard is written against. Regenerate it with `uv run python -m dataproof_api.openapi services/api/openapi.json`. A pre-commit hook and `tests/unit/test_openapi.py` fail if it is stale; the test also pins the list of routes.

## Running a deployment

A deployment is a trusted operator file plus secrets from the environment (`deployment.py`). [`deployment.example.toml`](deployment.example.toml) shows every setting.

- **The file** (its path in `DATAPROOF_CONFIG`) names the GitHub App (ID, private key file, OAuth client ID, API and web URLs), the dashboard's origin, the preview domain, the worker's data directory and Docker scope, run limits, and each registered repository: its installation and its trusted project configuration. Unknown keys are refused, so a secret cannot be put in the file by mistake. Each project's configuration is validated when the file loads. Origins must be `https://`, except a loopback dashboard during local use.
- **Secrets** come only from the environment, and each process reads only the ones it needs:

  | Variable | Used by |
  | --- | --- |
  | `DATAPROOF_DATABASE_URL` | every process |
  | `DATAPROOF_WEBHOOK_SECRET` (at least 16 characters) | `api` |
  | `DATAPROOF_SESSION_SECRET` (at least 32) | `api` |
  | `DATAPROOF_GITHUB_CLIENT_SECRET` | `api` |
  | `DATAPROOF_PREVIEW_SECRET` (at least 32) | `api`, `gateway` |

  A missing or short secret stops the process with the variable's name.
- **Derived, not configured:** each repository's dataset digest comes from its baseline, and the engine version from the installed runner.

Each process runs as `python -m dataproof_api <process>`:

| Process | What it does |
| --- | --- |
| `migrate` | Applies the control database's migrations |
| `api` | Webhook intake and the reviewer API (default `127.0.0.1:8000`) |
| `webhook` | Webhook intake alone (default `127.0.0.1:8001`): `POST /github/webhook` and no other route, not even the OpenAPI document. This is the only process a tunnel exposes. |
| `gateway` | The signed preview gateway (default `127.0.0.1:8080`), behind the preview domain's wildcard certificate |
| `worker` | Claims queued runs and reproduction requests and runs them on the local Docker backend. Each run uses its stored configuration, with the source broker fetching exact commits into `<data_dir>/mirror.git`. It times out overdue runs every 30 seconds |
| `publisher` | Publishes check runs to GitHub every few seconds |

The dashboard runs separately (`next start`) on the dashboard's origin, with `/api` and `/auth` routed to the `api` process.

Two commands manage the GitHub App, and only the owner runs them:

- `register-app --name … --webhook-url …` creates the App through GitHub's manifest flow, from a page served on the loopback. The App is private, with `checks: write`, read-only `contents`, `metadata` and `pull_requests`, and only `pull_request` events. Its key, webhook secret and client secret are written with owner-only permissions to `~/.dataproof/github-app/`, and none is printed.
- `webhook-url <tunnel address>` points the App's webhook at a new quick-tunnel address, as the App itself.

`[previews] port` puts a port in preview links. Locally, `previews.localhost` with port `8080` reaches the gateway. [`deployment.demo.toml`](deployment.demo.toml) is the template for the local live demo, and [`scripts/demo/stack.py`](../../scripts/demo/stack.py) starts every process; see [DEMO](../../docs/DEMO.md#live-setup).

No GitHub App is registered for this project. Everything here is tested against signed fixture events and a fake GitHub (`tests/github_env.py`) that verifies App JWTs against the App's public key and checks token scope and expiry. Nothing has talked to the real GitHub.

## Tests

- **`tests/unit/test_webhooks.py`:** signature verification, including altered bodies, wrong secrets, missing or `sha1=` signatures. Event parsing accepts people and apps (`dependabot[bot]`) and rejects malformed logins.
- **`tests/unit/test_github_app_readiness.py`:**
  - the App's manifest asks for the least DataProof needs;
  - the webhook process serves `POST /github/webhook` and nothing else;
  - registration accepts only its own state, stores the credentials, and never shows them;
  - the webhook follows a new tunnel address as the App;
  - each demo pull request changes only what DataProof executes.
- **`tests/unit/test_deployment.py`:**
  - the example deployment loads;
  - a secret or unknown key anywhere in the file is refused, and so is a non-loopback `http://` origin;
  - the API composes from the file and the environment, with digests and versions derived;
  - a missing or weak secret names its variable;
  - secrets are read only from the environment.
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
- **`tests/integration/test_reviewer_api.py`** (PostgreSQL, fake GitHub):
  - sign-in sets a signed `HttpOnly`, `Secure`, `SameSite=Lax` session and keeps no GitHub token;
  - a sign-in whose state came from elsewhere, or that has no state cookie, gets `400`, and a bad code gets `401`;
  - a missing, tampered or expired session gets `401`;
  - runs are listed only from readable repositories;
  - an unreadable run's `404` is identical to a missing run's;
  - run detail shows the author, transitions and check run;
  - checks show the binding a reviewer decides on;
  - losing access takes effect after the 60-second cache;
  - writes without the CSRF token or the dashboard's `Origin` get `403`;
  - a maintainer's approval starts a `binding_approval` run (`ORGANIZATIONAL`), is returned when repeated and blocks a contradicting rejection;
  - the author may approve only without another maintainer (`PROCEDURAL`), and otherwise gets `403` with nothing recorded;
  - read-only and unrelated logins cannot decide (`403`, `404`);
  - an unknown binding gets `422`, and a decision on a head the pull request has left gets `409`;
  - cancellation is recorded with the reviewer's decision and refused a second time;
  - a correction must be an existing, different commit, and starts a child run while the original stays queued;
  - a preview link is a grant for exactly that run, role and reviewer, refused to non-readers and without CSRF;
  - a reproduction is queued only for a failed check of a sealed run, once per check, and only by writers.
- **`tests/integration/test_reproduction_queue.py`** (PostgreSQL):
  - one request per check;
  - leased claims, with a crashed worker's attempt counted;
  - retries until the attempts are spent, and an immediate end when a request cannot succeed;
  - stale workers cannot end a job;
  - six concurrent workers claim three jobs exactly once;
  - requests are write-once.
- **`tests/integration/test_preview_gateway.py`** (PostgreSQL, fake edge; the negative tests #4 requires):
  - a grant opens the preview with a host-only cookie;
  - requests reach only the registered edge, with its token and without the reviewer's headers;
  - no cookie from the preview reaches the browser;
  - a missing, expired, tampered, other-run or other-role grant is denied with nothing sent upstream;
  - paths naming another host (`//evil.example`, `/http://169.254.169.254/…`, encoded `..`) never reach anything but the registered edge;
  - unknown runs and foreign domains are not previews, and a stopped preview gets `503`;
  - writes get `405`;
  - lost permission closes the preview;
  - redirects never leave it.
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
  - a signed F1 `pull_request` event goes through intake, a worker under the stored configuration and the publisher, and ends as a `failure` check run on F1's head commit (`queued`, then `completed`) with C1 failing in its table, and nothing on the base commit;
  - the real candidate preview, through the gateway with a grant, serves its API (stamped with the head commit) and its frontend page, and is `401` without one.
