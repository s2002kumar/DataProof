"""Run the demo against real GitHub on one machine, and record it.

    python scripts/demo/live.py --env ~/.dataproof/github-app/secrets.env [--record]

The live counterpart of ``rehearse.py``: nothing is simulated. It needs what docs/DEMO.md "Live
setup" prepares: ``DATAPROOF_CONFIG`` (the filled-in ``deployment.demo.toml``) and
``DATAPROOF_DATABASE_URL``; the demo repository pushed with its branches and the App installed
on it alone; ``gh`` signed in as someone with write access to it; ``cloudflared``.

1. The stack starts with a quick tunnel to the webhook-only intake, and the App's webhook
   follows it (``stack.py``). The pull request is opened only once GitHub can reach the intake.
2. Off camera, a browser window opens on the dashboard and the owner signs in with GitHub. Only
   the dashboard's own cookies are kept, and only in memory; GitHub's close with the window.
3. ``gh`` opens a pull request from ``ledger-backfill`` to ``main``, and GitHub's signed
   ``pull_request`` webhook starts the run.
4. The walk-through is the rehearsal's, and it also shows GitHub's own pages: the pull request
   with its DataProof check, and at the end the check runs on both commits.
5. The check runs GitHub holds for every run are written to ``evidence.json``.

On the way out the stack and the tunnel stop and every environment in the deployment's scope is
removed. The pull request stays open on GitHub as the record. Output goes to ``--out`` (default
``.dataproof-demo/live``).
"""

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from demo_walkthrough import LIVE, Walkthrough
from playwright.sync_api import StorageState, sync_playwright
from rehearse import DASHBOARD, SIZE, _writable, child, expect, release, removing_environments
from stack import TUNNEL_URL, environment, running

from dataproof_api.deployment import Deployment
from dataproof_contracts import RunTrigger
from dataproof_control import RunStore

REPO = Path(__file__).resolve().parents[2]
BRANCHES = ("main", "ledger-backfill", "ledger-backfill-direct")
TITLE = "Move legacy payment events into a per-order ledger"
BODY = (
    "Moves legacy payment events into a per-order ledger table: an Alembic backfill migration, "
    "the ledger version of the API and frontend, and DataProof bindings for the new table."
)
SIGNED_IN = re.compile(r"Signed in as ([A-Za-z0-9-]+)")


def gh(*arguments: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["gh", *arguments],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise SystemExit(f"gh {arguments[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def reachable(tunnel: str, seconds: float = 180) -> None:
    """Wait until GitHub's side of the tunnel reaches the intake, which refuses an unsigned post."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx2.HTTPError):
            if httpx2.post(f"{tunnel}/github/webhook", content=b"{}", timeout=10).status_code in (
                400,
                401,
                422,
            ):
                return
        time.sleep(2)
    raise SystemExit(f"{tunnel} did not reach the webhook intake")


def sign_in(timeout: float) -> tuple[StorageState, str]:
    """The owner signs in with GitHub in a visible window; the dashboard's session and login."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{DASHBOARD}/")
        page.get_by_role("link", name="Sign in with GitHub").click()
        print("SIGN IN: sign in with GitHub in the browser window that opened", flush=True)
        signed_in = page.get_by_text(SIGNED_IN)
        signed_in.wait_for(timeout=timeout * 1000)
        found = SIGNED_IN.search(signed_in.inner_text())
        state = context.storage_state()
        browser.close()
    if found is None:
        raise SystemExit("the dashboard did not say who signed in")
    host = urlsplit(DASHBOARD).hostname
    kept: StorageState = {
        "cookies": [c for c in state["cookies"] if c["domain"] == host],
        "origins": [o for o in state["origins"] if o["origin"] == DASHBOARD],
    }
    return kept, found.group(1)


def pull_request_run(dsn: str, repository: str, head: str, since: datetime) -> UUID:
    """The run GitHub's webhook started for the pull request's head commit."""
    store, deadline = RunStore(dsn), time.monotonic() + 180
    while time.monotonic() < deadline:
        for run in store.runs([repository]):
            if (
                run.trigger is RunTrigger.WEBHOOK
                and run.inputs.head_sha == head
                and run.created_at >= since
            ):
                print(f"GitHub's webhook started run {run.run_id}")
                return run.run_id
        time.sleep(2)
    raise SystemExit("no run started; see the webhook log and the App's recent deliveries")


def check_runs(repository: str, sha: str) -> list[dict[str, Any]]:
    listed = json.loads(
        gh(
            "api",
            f"repos/{repository}/commits/{sha}/check-runs?check_name=DataProof&filter=all"
            "&per_page=100",
        )
    )
    return [
        {
            "external_id": run["external_id"],
            "head_sha": run["head_sha"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "title": (run.get("output") or {}).get("title"),
            "html_url": run["html_url"],
            "app": run["app"]["slug"],
        }
        for run in listed["check_runs"]
    ]


def published(repository: str, sha: str, run_id: UUID, seconds: float = 120) -> dict[str, Any]:
    """Wait until GitHub holds the run's completed check run on ``sha``; that check run."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for check in check_runs(repository, sha):
            if check["external_id"] == str(run_id) and check["status"] == "completed":
                return check
        time.sleep(3)
    raise SystemExit(f"GitHub has no completed check run for {run_id}; see the publisher log")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, required=True, help="register-app's secrets.env")
    parser.add_argument("--record", action="store_true", help="record the walk-through")
    parser.add_argument("--out", type=Path, default=REPO / ".dataproof-demo" / "live")
    parser.add_argument("--sign-in-minutes", type=float, default=15)
    args = parser.parse_args(argv)
    env = environment(args.env)
    for required in ("DATAPROOF_CONFIG", "DATAPROOF_DATABASE_URL"):
        if not env.get(required):
            raise SystemExit(f"set {required}")
    deployment = Deployment.load(Path(env["DATAPROOF_CONFIG"]))
    if len(deployment.installations) != 1:
        raise SystemExit("the live demo expects one repository in the deployment")
    (repository,) = deployment.installations
    dsn = env["DATAPROOF_DATABASE_URL"]
    commits = {
        branch: gh("api", f"repos/{repository}/branches/{branch}", "-q", ".commit.sha")
        for branch in BRANCHES
    }
    if args.out.exists():
        shutil.rmtree(args.out, onexc=_writable)
    logs = args.out / "logs"
    evidence: dict[str, Any] = {"repository": repository, "commits": commits, "runs": []}

    def finished(run_id: UUID, verdict: str, name: str, head: str) -> float:
        waited = expect(dsn, run_id, verdict, name)
        check = published(repository, head, run_id)
        print(f"  GitHub check run: {check['conclusion']}, {check['title']} {check['html_url']}")
        evidence["runs"].append(
            {"name": name, "run_id": str(run_id), "verdict": verdict, "waited_s": round(waited)}
            | {"check_run": check}
        )
        return waited

    # The stack stops first (and its tunnel with it), then what the runs still hold is removed.
    with removing_environments(deployment.worker.scope), running(env, logs, tunnel=True):
        tunnel = TUNNEL_URL.search((logs / "tunnel.log").read_text("utf-8", errors="replace"))
        if tunnel is None:
            raise SystemExit("no tunnel address in the tunnel log")
        reachable(tunnel.group(0))
        state, reviewer = sign_in(args.sign_in_minutes * 60)
        print(f"signed in to the dashboard as {reviewer}")
        since = datetime.now(UTC)
        url = gh(
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            "main",
            "--head",
            "ledger-backfill",
            "--title",
            TITLE,
            "--body",
            BODY,
        ).splitlines()[-1]
        print("pull request:", url)
        evidence["pull_request"] = url
        head, direct = commits["ledger-backfill"], commits["ledger-backfill-direct"]
        first = pull_request_run(dsn, repository, head, since)
        walk = Walkthrough(
            args.out / "video" if args.record else None,
            SIZE,
            footer=LIVE,
            state=state,
            reviewer=reviewer,
        )
        try:
            walk.binding_review(first, finished(first, "INCOMPLETE", "run", head), url)
            approval = child(dsn, repository, first)
            release(dsn, repository, keep=approval, actor="live-demo")
            waited = finished(approval, "BLOCKED", "approval run", head)
            walk.blocked(approval, waited, direct)
            correction = child(dsn, repository, approval)
            release(dsn, repository, keep=correction, actor="live-demo")
            waited = finished(correction, "INCOMPLETE", "correction run", direct)
            walk.correction_review(correction, waited)
            corrected = child(dsn, repository, correction)
            release(dsn, repository, keep=corrected, actor="live-demo")
            waited = finished(corrected, "CHECKED", "corrected run", direct)
            blocked_check = evidence["runs"][1]["check_run"]["html_url"]
            checked_check = evidence["runs"][3]["check_run"]["html_url"]
            walk.checked(
                corrected,
                waited,
                [
                    (
                        blocked_check,
                        "On GitHub: the pull request's head commit keeps its BLOCKED check, "
                        "with the coverage of every required contract.",
                    ),
                    (
                        checked_check,
                        "And the corrected commit's check is CHECKED. Merging stays a "
                        "person's decision.",
                    ),
                ],
            )
        finally:
            walk.close()
        evidence["check_runs"] = {sha: check_runs(repository, sha) for sha in (head, direct)}
    (args.out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", "utf-8")
    print("evidence:", args.out / "evidence.json")
    if args.record:
        print("video:", walk.join(args.out / "live.mp4"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
