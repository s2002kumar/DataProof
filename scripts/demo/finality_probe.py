"""Validate the check-run publisher's finality against real GitHub (#78).

    python scripts/demo/finality_probe.py --env ~/.dataproof/github-app/secrets.env

#73 changed the publisher so that, on each commit, the newest run's check run is created last,
because a required check reads only the check run created last with its name on a commit
(probed on GitHub: docs/DEMO.md, "Check-run finality"). The fix was tested against a simulated
GitHub. This runs it against the real one, with the setup ``live.py`` uses, on new branches of
the demo repository; the README's record pull request is not touched.

1. **A burst of approvals** (the fail-closed bug #73 fixed). A pull request proposes five
   bindings and its first run waits for review. All five are approved within about a second,
   so approval runs supersede each other while queued. Required: the check run GitHub reads on
   the commit is the newest run's and ``success``, the pull request is mergeable, and superseded
   runs that never reached GitHub have no check run.
2. **An older run that turns green after a newer run exists** (the fail-open direction). On a
   second commit, the newest approval run is already working when a second pull request from
   the same commit, into another base branch, starts a newer run that will wait for review.
   Required: when the older run completes ``success``, the first pull request does not become
   mergeable, and once both are done GitHub reads the newer run's check run, not ``success``.

Every observation is written to ``--out``/evidence.json: each commit's check runs in the order
GitHub created them, the one GitHub reads, each pull request's mergeable state at each step,
and the runs and omissions in the control database. The one manual step is the GitHub sign-in
in the browser window that opens. On the way out the stack and the tunnel stop and every
environment in the deployment's scope is removed; the pull requests stay open as the record.
"""

import argparse
import json
import shutil
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import psycopg
from live import gh, pull_request_run, reachable, sign_in
from playwright.sync_api import StorageState
from psycopg.rows import dict_row
from rehearse import DASHBOARD, _writable, expect, release, removing_environments, settled
from stack import TUNNEL_URL, environment, running

from dataproof_api.deployment import Deployment
from dataproof_contracts import RunState
from dataproof_control import RunStore

REPO = Path(__file__).resolve().parents[2]
SOURCE = "ledger-backfill-direct"
"""The correct change set: with its bindings approved, a run is CHECKED."""
TITLE = "Finality probe: {test}"
BODY = (
    "Validates DataProof's check-run finality against real GitHub (DataProof #78). The change "
    "is the demo's correct ledger migration, on a new commit; see docs/DEMO.md in DataProof."
)
CHECK_NAME = "DataProof"


def fresh_commit(repository: str, source: str, branch: str, message: str) -> str:
    """A new commit with ``source``'s tree on a new branch: a new SHA, the same change."""
    tip = json.loads(gh("api", f"repos/{repository}/commits/{source}"))
    commit = json.loads(
        gh(
            "api",
            "-X",
            "POST",
            f"repos/{repository}/git/commits",
            "-f",
            f"message={message}",
            "-f",
            f"tree={tip['commit']['tree']['sha']}",
            "-f",
            f"parents[]={tip['sha']}",
        )
    )
    new_branch(repository, branch, str(commit["sha"]))
    return str(commit["sha"])


def new_branch(repository: str, branch: str, sha: str) -> None:
    gh("api", "-X", "POST", f"repos/{repository}/git/refs", "-f", f"ref=refs/heads/{branch}",
       "-f", f"sha={sha}")  # fmt: skip


def open_pull_request(repository: str, base: str, head: str, test: str) -> tuple[str, int]:
    url = gh(
        "pr",
        "create",
        "--repo",
        repository,
        "--base",
        base,
        "--head",
        head,
        "--title",
        TITLE.format(test=test),
        "--body",
        BODY,
    ).splitlines()[-1]
    print("pull request:", url, flush=True)
    return url, int(url.rstrip("/").rsplit("/", 1)[1])


def check_runs(repository: str, sha: str) -> list[dict[str, Any]]:
    """Every ``DataProof`` check run on ``sha``, in the order GitHub created them."""
    listed = json.loads(
        gh(
            "api",
            f"repos/{repository}/commits/{sha}/check-runs?check_name={CHECK_NAME}&filter=all"
            "&per_page=100",
        )
    )
    runs = [
        {
            "id": run["id"],
            "external_id": run["external_id"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "title": (run.get("output") or {}).get("title"),
            "html_url": run["html_url"],
        }
        for run in listed["check_runs"]
    ]
    return sorted(runs, key=lambda run: int(run["id"]))


def read_by_github(repository: str, sha: str) -> dict[str, Any] | None:
    """The check run a required ``DataProof`` check reads on ``sha``: GitHub's latest."""
    listed = json.loads(
        gh(
            "api",
            f"repos/{repository}/commits/{sha}/check-runs?check_name={CHECK_NAME}&filter=latest",
        )
    )
    runs = listed["check_runs"]
    if not runs:
        return None
    run = runs[0]
    return {
        "id": run["id"],
        "external_id": run["external_id"],
        "status": run["status"],
        "conclusion": run["conclusion"],
        "html_url": run["html_url"],
    }


def mergeable_state(repository: str, number: int, seconds: float = 90) -> str:
    """GitHub's verdict on whether the pull request may merge (``clean`` means it may)."""
    deadline = time.monotonic() + seconds
    state = "unknown"
    while time.monotonic() < deadline:
        state = gh("api", f"repos/{repository}/pulls/{number}", "-q", ".mergeable_state")
        if state != "unknown":
            return state
        time.sleep(3)
    return state


def control_runs(dsn: str, head: str) -> list[dict[str, Any]]:
    """The control database's runs on ``head`` and the check runs it omitted, oldest first."""
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT r.run_id::text, r.pull_request, r.trigger, r.state, "
            "r.superseded_by::text AS superseded_by, r.created_at, "
            "o.newer_run_id::text AS omitted_for "
            "FROM runs AS r LEFT JOIN github_check_omissions AS o USING (run_id) "
            "WHERE r.head_sha = %s ORDER BY r.created_at, r.run_id",
            (head,),
        ).fetchall()
    return [{**row, "created_at": row["created_at"].isoformat()} for row in rows]


class Reviewer:
    """The signed-in reviewer's dashboard session, used to decide bindings as the UI does."""

    def __init__(self, state: StorageState) -> None:
        self.client = httpx2.Client(base_url=DASHBOARD, timeout=60)
        for cookie in state["cookies"]:
            self.client.cookies.set(
                cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"]
            )
        session = self.client.get("/api/session")
        session.raise_for_status()
        self.csrf = str(session.json()["csrf_token"])

    def awaiting(self, run_id: UUID) -> list[str]:
        """The digests of the bindings a run's checks wait to have reviewed."""
        checks = self.client.get(f"/api/runs/{run_id}/checks")
        checks.raise_for_status()
        digests = []
        for record in checks.json():
            if (
                record["binding_digest"]
                and record["result"]["not_run_reason"] == "AWAITING_BINDING_REVIEW"
                and record["binding_digest"] not in digests
            ):
                digests.append(record["binding_digest"])
        return digests

    def approve(self, run_id: UUID, digest: str) -> UUID:
        """Approve one binding; the run the approval started."""
        response = self.client.post(
            f"/api/runs/{run_id}/binding-decisions",
            json={"action": "APPROVE_BINDING", "binding_digest": digest},
            headers={"X-CSRF-Token": self.csrf, "Origin": DASHBOARD},
        )
        response.raise_for_status()
        return UUID(response.json()["started_run_id"])

    def approve_all(self, run_id: UUID) -> tuple[list[UUID], float]:
        """Approve every binding the run waits on, as fast as the API allows."""
        digests = self.awaiting(run_id)
        started = time.monotonic()
        runs = [self.approve(run_id, digest) for digest in digests]
        burst = time.monotonic() - started
        print(f"approved {len(digests)} bindings in {burst:.2f} s", flush=True)
        return runs, burst


def published_status(repository: str, sha: str, run_id: UUID, seconds: float = 180) -> str:
    """Wait until GitHub holds a check run for the run on ``sha``; its status."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for check in check_runs(repository, sha):
            if check["external_id"] == str(run_id):
                return str(check["status"])
        time.sleep(3)
    raise SystemExit(f"GitHub has no check run for {run_id}; see the publisher log")


def completed_on_github(repository: str, sha: str, run_id: UUID, seconds: float = 180) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for check in check_runs(repository, sha):
            if check["external_id"] == str(run_id) and check["status"] == "completed":
                return
        time.sleep(3)
    raise SystemExit(f"the check run for {run_id} did not complete on GitHub")


def free(dsn: str, run_id: UUID) -> None:
    """Cancel a run that has reported and waits in review, to release its previews."""
    if RunStore(dsn).cancel(run_id, actor="finality-probe", reason="released"):
        print(f"released run {run_id}", flush=True)


def working(dsn: str, run_id: UUID, seconds: float = 300) -> str:
    """Wait until a worker has claimed the run; its state."""
    store, deadline = RunStore(dsn), time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = store.get_run(run_id).state
        if state is not RunState.QUEUED:
            return state.value
        time.sleep(1)
    raise SystemExit(f"no worker claimed {run_id}")


def burst(
    dsn: str, repository: str, reviewer: Reviewer, stamp: str, evidence: dict[str, Any]
) -> None:
    branch = f"finality-burst-{stamp}"
    head = fresh_commit(repository, SOURCE, branch, f"Finality probe, burst of approvals ({stamp})")
    since = datetime.now(UTC)
    url, number = open_pull_request(repository, "main", branch, "a burst of approvals")
    first = pull_request_run(dsn, repository, head, since)
    expect(dsn, first, "INCOMPLETE", "first run")
    completed_on_github(repository, head, first)
    approvals, seconds = reviewer.approve_all(first)
    free(dsn, first)  # its previews; the approval runs are left to race as they would
    newest = approvals[-1]
    expect(dsn, newest, "CHECKED", "newest approval run")
    for run in approvals[:-1]:
        settled(dsn, run)
    completed_on_github(repository, head, newest)
    test: dict[str, Any] = {
        "pull_request": url,
        "head_sha": head,
        "first_run": str(first),
        "approval_runs": [str(run) for run in approvals],
        "approval_seconds": round(seconds, 2),
        "newest_run": str(newest),
        "check_runs": check_runs(repository, head),
        "read_by_github": read_by_github(repository, head),
        "mergeable_state": mergeable_state(repository, number),
        "control_runs": control_runs(dsn, head),
    }
    read = test["read_by_github"]
    test["passed"] = bool(
        read
        and read["external_id"] == str(newest)
        and read["conclusion"] == "success"
        and test["mergeable_state"] == "clean"
    )
    print(f"burst: GitHub reads {read}; mergeable {test['mergeable_state']}", flush=True)
    evidence["burst"] = test
    release(dsn, repository, keep=newest, actor="finality-probe")
    RunStore(dsn).cancel(newest, actor="finality-probe", reason="released")


def older_green_after_newer(
    dsn: str, repository: str, reviewer: Reviewer, stamp: str, evidence: dict[str, Any]
) -> None:
    branch = f"finality-race-{stamp}"
    other_base = f"finality-base-{stamp}"
    head = fresh_commit(repository, SOURCE, branch, f"Finality probe, older green ({stamp})")
    new_branch(repository, other_base, gh("api", f"repos/{repository}/commits/main", "-q", ".sha"))
    since = datetime.now(UTC)
    url, number = open_pull_request(repository, "main", branch, "an older run turns green late")
    first = pull_request_run(dsn, repository, head, since)
    expect(dsn, first, "INCOMPLETE", "first run")
    completed_on_github(repository, head, first)
    approvals, seconds = reviewer.approve_all(first)
    older = approvals[-1]
    free(dsn, first)
    claimed_as = working(dsn, older)
    older_status = published_status(repository, head, older)
    print(f"older run {older} is {claimed_as}; its check run is {older_status}", flush=True)
    since = datetime.now(UTC)
    other_url, other_number = open_pull_request(
        repository, other_base, branch, "a newer run on the same commit"
    )
    newer = pull_request_run(dsn, repository, head, since)
    store = RunStore(dsn)
    at_newer: dict[str, Any] = {
        "older_state": store.get_run(older).state.value,
        "check_runs": check_runs(repository, head),
    }
    expect(dsn, older, "CHECKED", "older approval run")
    completed_on_github(repository, head, older)
    at_older_green: dict[str, Any] = {
        "newer_state": store.get_run(newer).state.value,
        "read_by_github": read_by_github(repository, head),
        "mergeable_state": mergeable_state(repository, number),
    }
    print(f"when the older run was green: {at_older_green}", flush=True)
    expect(dsn, newer, "INCOMPLETE", "newer run")
    completed_on_github(repository, head, newer)
    test: dict[str, Any] = {
        "pull_request": url,
        "other_pull_request": other_url,
        "head_sha": head,
        "first_run": str(first),
        "approval_runs": [str(run) for run in approvals],
        "approval_seconds": round(seconds, 2),
        "older_run": str(older),
        "older_run_when_newer_started": {"state": claimed_as, "check_run": older_status},
        "newer_run": str(newer),
        "when_newer_run_started": at_newer,
        "when_older_run_was_green": at_older_green,
        "check_runs": check_runs(repository, head),
        "read_by_github": read_by_github(repository, head),
        "mergeable_state": mergeable_state(repository, number),
        "other_mergeable_state": mergeable_state(repository, other_number),
        "control_runs": control_runs(dsn, head),
    }
    read = test["read_by_github"]
    green = at_older_green["read_by_github"]
    test["passed"] = bool(
        read
        and read["external_id"] == str(newer)
        and read["conclusion"] != "success"
        and green
        and green["external_id"] == str(newer)
        and at_older_green["mergeable_state"] != "clean"
        and test["mergeable_state"] != "clean"
    )
    print(f"older green: GitHub reads {read}; mergeable {test['mergeable_state']}", flush=True)
    evidence["older_green_after_newer"] = test
    release(dsn, repository, keep=newer, actor="finality-probe")
    RunStore(dsn).cancel(newer, actor="finality-probe", reason="released")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", type=Path, required=True, help="register-app's secrets.env")
    parser.add_argument("--out", type=Path, default=REPO / ".dataproof-demo" / "finality")
    parser.add_argument("--sign-in-minutes", type=float, default=15)
    args = parser.parse_args(argv)
    env = environment(args.env)
    for required in ("DATAPROOF_CONFIG", "DATAPROOF_DATABASE_URL"):
        if not env.get(required):
            raise SystemExit(f"set {required}")
    deployment = Deployment.load(Path(env["DATAPROOF_CONFIG"]))
    if len(deployment.installations) != 1:
        raise SystemExit("the probe expects one repository in the deployment")
    (repository,) = deployment.installations
    dsn = env["DATAPROOF_DATABASE_URL"]
    if args.out.exists():
        shutil.rmtree(args.out, onexc=_writable)
    logs = args.out / "logs"
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    evidence: dict[str, Any] = {"repository": repository, "stamp": stamp}
    with removing_environments(deployment.worker.scope), running(env, logs, tunnel=True):
        tunnel = TUNNEL_URL.search((logs / "tunnel.log").read_text("utf-8", errors="replace"))
        if tunnel is None:
            raise SystemExit("no tunnel address in the tunnel log")
        reachable(tunnel.group(0))
        state, login = sign_in(args.sign_in_minutes * 60)
        print(f"signed in to the dashboard as {login}", flush=True)
        evidence["reviewer"] = login
        reviewer = Reviewer(state)
        try:
            burst(dsn, repository, reviewer, stamp, evidence)
            older_green_after_newer(dsn, repository, reviewer, stamp, evidence)
        finally:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "evidence.json").write_text(
                json.dumps(evidence, indent=2, default=str) + "\n", "utf-8"
            )
            print("evidence:", args.out / "evidence.json", flush=True)
    names = ("burst", "older_green_after_newer")
    passed = [name for name in names if evidence.get(name, {}).get("passed")]
    print(f"passed: {passed}", flush=True)
    return 0 if len(passed) == len(names) else 1


if __name__ == "__main__":
    sys.exit(main())
