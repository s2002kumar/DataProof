# DEMO — 90-Second Walkthrough

## One-line pitch

> DataProof tests migrations in isolated copies of an application, catches silent data corruption, and shows evidence-backed checks and rule-based explanations for human review.

## Script and artifacts

**Recorded-versus-proposed boundary:** The published demonstration uses rule-based,
model-off diagnosis. The AI investigator has scripted-model tests but its
live-model comparative value is not established. Do not describe the video as
showing an AI tool trace or a real-model improvement.

**0–12s — Show the problem:** Open GitHub PR changing commerce orders/refunds SQL. Point at green SQL execution alone not proving data correctness.

**12–30s — Live product:** DataProof shows reference and candidate preview URLs early. Demonstrate actual frontend/API/PG, identical baseline digest, and run SHA. When the candidate introduces its ledger table/endpoint mapping, the first run completes `INCOMPLETE` (non-green); show the explicit **`APPROVE_BINDING`** human action as a short annotated step, then the new run it starts. No step silently blesses a PR-provided comparator.

**30–50s — Valid migration:** Select a second safe migration or cached completed safe run (explicitly label recorded vs live). Its declared invariants and selected API behavior checks pass; show exact coverage, not a generic 10/10.

**50–72s — Silent corruption:** F1 grouped-join migration **successfully executes**, preserves row count and event key identity, but changes a $49.99 refund to a single $99.98 ledger entry. Show exact event ID, affected order, source SQL and failing C1/C2. Note that a configured amount-SUM baseline would also catch this particular fault; use F3 in benchmark to show more discriminating value-check benefits.

**72–85s — Evidence-backed diagnosis:** Show rule-based mismatch diagnosis and its cited evidence. If a verified smaller fixture exists, show it replaying the same failure. Do not call this a live AI investigation.

**85–90s — Developer decision:** Show GitHub commit-bound status and downloadable evidence report; remind that production merge/deploy remains human-controlled.

## Evidence the published video must contain

A real uncropped path or code-commit link proving DB migration ran; actual preview app; deterministic mismatches; evidence citations; check results; GitHub commit SHA; a clean rerun and resource cleanup. Clearly annotate any prerecorded steps or accelerated playback. Do not fabricate UI or metric screenshots.

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
4. **Demo repository (owner):** run `python scripts/demo/build_demo_repo.py <empty dir>`. Then push it to a new repository, for example with `gh repo create <owner>/<name> --public --source <dir> --push`, followed by `git push origin ledger-backfill ledger-backfill-direct`. A public repository lets anyone open the pull request and its check runs. It does not let strangers start runs: a pull request from a fork is refused before anything runs (`NOT_EXECUTED: FORK_PULL_REQUEST`), so only people with write access can start one.
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

Status: done. The project's App is registered and installed on [s2002kumar/dataproof-demo-commerce](https://github.com/s2002kumar/dataproof-demo-commerce) alone, and the review has run live ([Live run](#live-run)).

## Live run

`scripts/demo/live.py` runs the review above end to end against real GitHub, and can record it:

```bash
uv run python scripts/demo/live.py --env ~/.dataproof/github-app/secrets.env --record
```

It needs setup steps 1 to 6, `DATAPROOF_CONFIG` and `DATAPROOF_DATABASE_URL`, and `gh` signed in as someone with write access to the demo repository.

- **The stack:** it starts the stack with a quick tunnel, as in step 7. It opens the pull request only once GitHub can reach the intake, because GitHub does not retry a failed delivery by itself.
- **Sign-in, off camera:** a browser window opens for the owner to sign in with GitHub. The script keeps only the dashboard's session cookie, in memory. GitHub's cookies close with the window.
- **The review:** it opens the pull request with `gh` and runs the rehearsal's walk-through. The walk-through also shows GitHub's own pages: the pull request with its check, and at the end the check runs on both commits.
- **Output:** the check run GitHub holds for every run goes to `evidence.json`, and the recording to `live.mp4`, under `.dataproof-demo/live`.
- **On the way out:** the stack and the tunnel stop, and every environment in the deployment's scope is removed. The pull request stays open as the record.

Approvals are bound to a commit and a binding digest, so a second take on the same commits would start out approved. For a new take, give the demo branches new commits with the same content (for example `git commit --amend --no-edit` on each, then a force push), and the script opens a new pull request.

**Recorded on 2026-11-03** ([recording](media/live-demo.mp4)), on [pull request #2](https://github.com/s2002kumar/dataproof-demo-commerce/pull/2):

| Run | Commit | Verdict | Wait, off camera | GitHub check |
| --- | --- | --- | --- | --- |
| Started by the pull request's webhook | `9073d40`, the head | `INCOMPLETE` | 123 s | [`action_required`](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126166567317) |
| Carries the five approvals | `9073d40` | `BLOCKED` | 106 s | [`failure`](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126168145938) |
| Correction replay | `2e49a55`, the corrected commit | `INCOMPLETE` | 73 s | [`action_required`](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126169394549) |
| Carries the five approvals | `2e49a55` | `CHECKED` | 91 s | [`success`](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126170290869) |

- **What failed:** in the `BLOCKED` run, C1 found 300 mismatches, C2 79, C3 141, and the two C6 API checks 220 and 110. The migration itself and the candidate preview passed.
- **The other check runs:** approval runs that a later approval superseded while queued are on GitHub as `cancelled`, "Superseded by a newer run". So is the one approval run a worker claimed before the remaining approvals arrived; the script released it ("Cancelled before it was verified"). One of the superseded runs' check runs is the one GitHub reads as the corrected commit's latest. That was a publisher bug, now fixed: see [Check-run finality](#check-run-finality).
- **One person:** the pull request's author is also the reviewer and the repository's only maintainer, so every approval is recorded with procedural separation.
- **Cleanup:** after the run, no DataProof container, network or volume was left.
- **The first take,** pull request #1, ran correctly through the `BLOCKED` run. Then the recording script, not DataProof, took an approval run that a later approval superseded. The script now takes each approval's run from the dashboard. #1 is closed with that explanation.

### Check-run finality

After the live run, GitHub's default (latest) check-run query on the corrected commit `2e49a55` returned a superseded approval run's `cancelled` check run, [126170298696](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126170298696). It did not return the `CHECKED` one, [126170290869](https://github.com/s2002kumar/dataproof-demo-commerce/runs/126170290869).

**The cause:**

- Five approvals within about two seconds started five approval runs on the commit. Each one superseded the queued one before it.
- Three of those runs were superseded before the publisher's next pass. The publisher worked through runs least recently changed first, and superseding a run changes it.
- So the publisher created the newest run's check run first, then a check run for a superseded run, created already `cancelled`. Each superseded run got a check run that said nothing new.

**GitHub's rule.** To test it rather than assume it, the demo repository's `main` now requires a `DataProof` check from the DataProof App, with administrators included. Labelled probe check runs, not verifications, were created on throwaway commits, each with a pull request. The probe ran on 2026-11-03; its pull requests (#3 to #7) are closed and its branches deleted.

| Check runs named `DataProof` on one commit, in the order created | Read by the required check | Pull request |
| --- | --- | --- |
| `success`, then `cancelled` | `cancelled` | blocked |
| `cancelled`, then `success` | `success` | mergeable |
| A, then B, both queued; then B completed `failure` and A `success` | B, `failure` | blocked |
| `success`, then `cancelled` with `started_at` an hour earlier | `cancelled` | blocked |
| The live run's `2e49a55`, as DataProof left it | the superseded run's `cancelled` | blocked |

- **What the rule is:** a required check reads only the check run created last with its name on the commit. The order check runs complete in does not matter, a backdated `started_at` does not matter, and an earlier success does not count.
- **What it meant here:** a commit DataProof had `CHECKED` was blocked.
- **It could also fail open:** an older run that reported `CHECKED` while the publisher was behind would get its check run created after a newer run's. Its `success` would then hide the newer run's `BLOCKED`. Regression tests reproduce both cases on the old publisher.
- **The fix:** on each commit, the newest run's check run is now created last.
  - A run that has not reached GitHub when a newer pull request run exists on its commit gets no check run. That is recorded once, and the dashboard links the newer run in its place.
  - A commit's check runs are created one at a time, under a lock per commit.
  - A check run that GitHub already shows is still brought to completion. Every interrupted run is still never green.
- **The demo commits keep their check runs:** GitHub cannot delete a check run, so `2e49a55` still reads as blocked. A new take would run on new commits with the fixed publisher.
- **Not handled:** check runs belong to a commit, not to a pull request. Two pull requests with the same head commit, for example into two base branches, share one latest `DataProof` check, which is the newer run's. Whether a check per base branch is worth it is an open question.

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
- **Screenshots:** with `--screenshots <dir>`, each run page the walk-through opens is also saved full-page at desktop and phone widths, without the caption bar. The [dashboard's README](../services/dashboard/README.md#pages) shows crops from them.
- **On 2026-11-03,** two rehearsals ran on the five-step review page, every walk-through step passing. The runs were `INCOMPLETE` in 88 s and 67 s, `BLOCKED` in 83 s and 155 s, the correction `INCOMPLETE` in 59 s and 73 s, and `CHECKED` in 92 s and 116 s. Each commit received two check runs, the newest run's created last, and nothing was left behind.
- **The recording:** with `--record`, every step is recorded and the steps are joined into `.dataproof-demo/rehearsal/rehearsal.mp4` ([a recording](media/rehearsal.mp4)). Joining needs `ffmpeg` on the path, or `FFMPEG` set. Waiting for a run happens off camera, and each step opens with a card that says how long the wait took. Captions are drawn as a subtitle bar that is clearly not part of the product. Nothing is sped up or staged.

## README ordering after implementation

Hero line → 30–90s demo → one screenshot for failing integrity report → short 'How it works' diagram → local quickstart → test & benchmark outcomes → tradeoffs and limitations → source credit. A first-time reader can understand it in 10 seconds and inspect engineering depth afterward.
