# DataProof control plane API

The control plane's HTTP surface (ARCHITECTURE, GitHub connector and API surface). It is being built in work package 9 (#47). Implemented so far: GitHub webhook intake.

## Webhook intake

`POST /github/webhook` turns verified GitHub pull request events into runs.

1. **Signature first.** `X-Hub-Signature-256` must be the HMAC-SHA256 of the exact raw body under the shared webhook secret, compared in constant time. Anything else is `401`, and nothing is recorded. Bodies over 5 MiB are `413`; a signed body that is not a JSON object is `400`.
2. **Recorded once.** Each verified delivery is recorded write-once in `github_deliveries` with what intake did about it. A redelivery is answered from that record (`duplicate: true`).
3. **Trusted only as far as the signature goes.** The repository must be one the operator registered, the event must come from the GitHub App installation registered for it, and the pull request must target that repository. Otherwise the delivery is `IGNORED` or `REFUSED`, and no run is created.
4. **Pinned runs.** `opened`, `reopened`, `synchronize` and `ready_for_review` create a `webhook` run pinned to the event's exact base and head SHAs and to the operator's trusted configuration, which is stored under its digest so a worker can load it. Nothing the pull request says (title, body, branch names) is used. Runs are idempotent on their inputs, so a redelivered or repeated event finds its run (`RUN_EXISTS`). A new head supersedes the runs for older heads.
5. **Forks.** A fork's pull request becomes a run with `head_repository` set, which `ANALYZING` refuses as `NOT_EXECUTED: FORK_PULL_REQUEST` before anything is provisioned. It is reported, not silently dropped.
6. **Closing.** `closed` cancels the pull request's working and reviewable runs, which are then cleaned up.

The pull request's author and head repository are recorded per head in `github_pull_request_heads`; the approver rule (ADR-0005) needs the author.

## Not built yet

These are the remaining steps of work package 9 (#47):

- GitHub App authentication;
- commit-bound check runs;
- the source broker that fetches exact commits;
- the reviewer API with sign-in and authorization;
- the signed preview gateway;
- the dashboard.

No GitHub App is registered for this project. Intake is tested only with signed fixture events.

## Tests

- **`tests/unit/test_webhooks.py`:** signature verification, including altered bodies, wrong secrets, missing or `sha1=` signatures. Event parsing accepts people and apps (`dependabot[bot]`) and rejects malformed logins.
- **`tests/integration/test_github_intake.py`** (PostgreSQL), signed deliveries through the FastAPI app:
  - pinned runs, the stored configuration, the recorded author and the actor on the first transition;
  - redeliveries, and supersession by a new head;
  - forged deliveries, which record nothing;
  - unconfigured repositories, foreign installations and cross-repository pull requests;
  - forks and closing;
  - signed bodies that are not events.
- **`tests/integration/test_pipeline.py`** (Docker): a fork's run is `NOT_EXECUTED` (neutral) and nothing is provisioned.
