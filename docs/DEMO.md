# DEMO — 90-Second Walkthrough

## One-line pitch

> DataProof tests database migrations in isolated copies of an application, catches silent data corruption, and produces evidence-backed AI explanations before a developer approves the change.

## Script and artifacts

**0–12s — Show the problem:** Open GitHub PR changing commerce orders/refunds SQL. Point at green SQL execution alone not proving data correctness.

**12–30s — Live product:** DataProof shows reference and candidate preview URLs early. Demonstrate actual frontend/API/PG, identical baseline digest, and run SHA. When the candidate introduces its ledger table/endpoint mapping, the first run completes `INCOMPLETE` (non-green); show the explicit **`APPROVE_BINDING`** human action as a short annotated step, then the new run it starts. No step silently blesses a PR-provided comparator.

**30–50s — Valid migration:** Select a second safe migration or cached completed safe run (explicitly label recorded vs live). Its declared invariants and selected API behavior checks pass; show exact coverage, not a generic 10/10.

**50–72s — Silent corruption:** F1 grouped-join migration **successfully executes**, preserves row count and event key identity, but changes a $49.99 refund to a single $99.98 ledger entry. Show exact event ID, affected order, source SQL and failing C1/C2. Note that a configured amount-SUM baseline would also catch this particular fault; use F3 in benchmark to show more discriminating value-check benefits.

**72–85s — AI investigation:** Open grounded analysis with cited query evidence and hypothesis. If a verified smaller fixture exists, show it replaying the same failure. Optionally demonstrate developer-selected correction replay in a recorded clip.

**85–90s — Developer decision:** Show GitHub commit-bound status and downloadable evidence report; remind that production merge/deploy remains human-controlled.

## Evidence the published video must contain

A real uncropped path or code-commit link proving DB migration ran; actual preview app; deterministic mismatches; AI tool trace; check results; GitHub commit SHA; a clean rerun and resource cleanup. Clearly annotate any prerecorded steps or accelerated playback. Do not fabricate UI or metric screenshots.

## Live setup

The demo runs on one machine. Everything is on the loopback except one route: GitHub's webhook reaches the webhook-only intake (`POST /github/webhook`, no other route, signatures verified) through a temporary Cloudflare quick tunnel. Reviewers sign in with GitHub, and the OAuth callback returns to the loopback dashboard. Run containers never receive a GitHub token, the App's key, or a control-plane credential.

Steps marked **owner** create or use credentials and are run by the repository owner.

1. **Prerequisites:** Docker, `uv`, `pnpm`, Node 24, and [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) (`winget install Cloudflare.cloudflared`). Quick tunnels need no Cloudflare account.
2. **Control database:**

   ```bash
   docker run -d --name dataproof-control -p 127.0.0.1:5433:5432 -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-alpine
   ```

   Then set `DATAPROOF_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/postgres`.
3. **Runtime image:** run `docker build --quiet --file infra/docker/runtime/Dockerfile .` and keep the printed `sha256:` image ID.
4. **Demo repository (owner):** run `python scripts/demo/build_demo_repo.py <empty dir>`. Then push it to a new repository, for example with `gh repo create <owner>/<name> --private --source <dir> --push`, followed by `git push origin ledger-backfill ledger-backfill-direct`.
5. **GitHub App (owner):** start a tunnel first, so the App has a webhook address:

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8001
   ```

   Then run:

   ```bash
   uv run python -m dataproof_api register-app --name "<unique name>" --webhook-url https://<tunnel>/github/webhook
   ```

   Open `http://127.0.0.1:8765/` and create the App. It is private, has checks write and read-only contents, metadata and pull requests, and receives only `pull_request` events. Its key and secrets go to `~/.dataproof/github-app/` and are never printed. `secrets.env` there also holds freshly generated session and preview-grant secrets, so it and `DATAPROOF_DATABASE_URL` are every secret the stack needs. **Install it on the demo repository only.**
6. **Deployment (owner):** copy [`services/api/deployment.demo.toml`](../services/api/deployment.demo.toml), fill in the App ID, client ID, installation ID, demo repository and runtime image, and set `DATAPROOF_CONFIG` to it.
7. **Start the stack:**

   ```bash
   uv run python scripts/demo/stack.py --env ~/.dataproof/github-app/secrets.env --tunnel
   ```

   This migrates and serves the dashboard on `http://127.0.0.1:3000`, the API, the webhook intake, the gateway, a worker, the publisher and the reaper. It opens a new quick tunnel to the webhook port alone and points the App's webhook at it.
8. **The review:** open a pull request from `ledger-backfill` to `main` in the demo repository.
   1. DataProof's check appears on the head commit, and the previews build.
   2. The first run is `INCOMPLETE` (`action_required`): the pull request proposes new bindings.
   3. In the dashboard, sign in, inspect the binding and approve it. The approval run is `BLOCKED` (`failure`), and C1 shows refund 1002 at 49.99 expected and 99.98 observed.
   4. The diagnosis cites its evidence, the candidate preview shows the same refund, and the reviewer decides.

Status: everything above except the owner steps is implemented and tested locally (#63). The live run, and the recording made from it, wait for the owner's App registration.

## Local rehearsal

`scripts/demo/rehearse.py` runs the same review on one machine, with no credentials:

```bash
uv run python scripts/demo/rehearse.py --image <runtime image ID> --record
```

- **Real:** everything DataProof does. That covers:
  - a control database, and webhook intake that verifies signatures;
  - a worker that migrates isolated Docker copies and builds both previews behind the signed gateway;
  - the contracts, the diagnosis and the check-run publisher;
  - the dashboard, driven in Chromium by Playwright.
- **Simulated: GitHub only.** The test double from `tests/github_env.py` issues App tokens, signs the reviewer in as `maintainer` and records check runs. The pull request's commits come from a local bare repository built by `build_demo_repo.py`, through the deployment's `git_url` (`file://`).
- **The walk-through:**
  1. The first run is `INCOMPLETE`: the pull request proposes five bindings. The reviewer approves each one, and one new run carries all five approvals.
  2. That run is `BLOCKED`, with refund 1002 at 49.99 expected and 99.98 observed. The reviewer opens both previews, reads the cited diagnosis, and replays the corrected commit (`ledger-backfill-direct`).
  3. The correction run is `INCOMPLETE`. Approvals are bound to a commit and a binding digest, so the reviewer approves the corrected commit's bindings.
  4. The corrected run is `CHECKED`.

  The script then prints the check runs that GitHub received.
- **Between steps** the script cancels, off camera, the runs the walk-through has finished with. A run in review holds its databases and previews until its review expires, and one machine's Docker address pool holds only a few runs. Their reports and decisions stand.
- **The recording:** with `--record`, every step is recorded and the steps are joined into `.dataproof-demo/rehearsal/rehearsal.mp4`. Joining needs `ffmpeg` on the path, or `FFMPEG` set. Waiting for a run happens off camera, and each step opens with a card that says how long the wait took. Captions are drawn as a subtitle bar that is clearly not part of the product. Nothing is sped up or staged.

## README ordering after implementation

Hero line → 30–90s demo → one screenshot for failing integrity report → short 'How it works' diagram → local quickstart → test & benchmark outcomes → tradeoffs and limitations → source credit. A first-time reader can understand it in 10 seconds and inspect engineering depth afterward.
