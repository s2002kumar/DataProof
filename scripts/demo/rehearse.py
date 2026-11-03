"""Rehearse the demo on one machine, with GitHub simulated, and record it.

    python scripts/demo/rehearse.py --image <runtime image ID> [--record]

Everything DataProof does here is real: the control database, webhook intake, a worker running
the migration in isolated Docker environments, both previews behind the signed gateway, the
contracts, the diagnosis, the check-run publisher and the dashboard in a browser. **Only GitHub
is simulated**, by the test double the integration and browser tests use (``tests/github_env.py``):
it issues App tokens, signs the reviewer in as ``maintainer``, answers permission checks and
receives check runs. The pull request's commits are fetched from a local bare repository that
``build_demo_repo.py`` builds, through the deployment's ``git_url``.

The walk-through is the live demo's (docs/DEMO.md):

1. a signed ``pull_request`` webhook for ``ledger-backfill``; the run is ``INCOMPLETE``
   because the pull request proposes new bindings;
2. the reviewer signs in, opens the candidate preview, and approves the binding;
3. the approval run is ``BLOCKED``: C1 shows refund 1002 at 49.99 expected, 99.98 observed, and
   the diagnosis cites its evidence;
4. the reviewer replays the corrected commit (``ledger-backfill-direct``), which is ``CHECKED``;
5. the check runs GitHub received, one per run, are printed.

With ``--record``, each step is recorded in Chromium (Playwright) and the steps are joined into
one video. Waiting for a run happens off camera, and each step opens with a card that says how
long the wait was. Nothing is sped up. Output goes to ``--out`` (default
``.dataproof-demo/rehearsal``).
"""

import argparse
import contextlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
import httpx2
import psycopg
import uvicorn

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from demo_walkthrough import Walkthrough  # noqa: E402
from stack import environment, running  # noqa: E402

from dataproof_api import sign  # noqa: E402
from dataproof_contracts import RunState  # noqa: E402
from dataproof_control import RunStore  # noqa: E402
from dataproof_control.results import RunResults  # noqa: E402
from dataproof_runner.local_docker import LocalDockerBackend  # noqa: E402
from github_env import (  # noqa: E402
    APP_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    PRIVATE_KEY_PEM,
    FakeGitHub,
    FakeGitHubServer,
)

REPOSITORY = "demo-org/commerce-orders"
INSTALLATION = 7001
DASHBOARD = "http://127.0.0.1:3000"
WEBHOOK = "http://127.0.0.1:8001/github/webhook"
GITHUB_PORT = 18901
CONTROL_PORT = 55433
CONTROL = "dataproof-rehearsal-control"
SCOPE = "rehearsal"
SIZE = {"width": 1280, "height": 720}
SETTLED = {
    RunState.READY_FOR_REVIEW,
    RunState.COMPLETE,
    RunState.ERROR,
    RunState.SUPERSEDED,
    RunState.NOT_EXECUTED,
    RunState.TIMED_OUT,
    RunState.CANCELLED,
}
"""Ready for review (previews still up), finished, or failed: the run will not move on its own."""


def git(directory: Path, *arguments: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *arguments],  # noqa: S607
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def demo_repository(out: Path) -> dict[str, str]:
    """The demo repository as a bare remote under ``out/git``; the commit of each branch."""
    source = out / "source"
    subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "demo" / "build_demo_repo.py"), str(source)],
        check=True,
    )
    bare = out / "git" / f"{REPOSITORY}.git"
    bare.parent.mkdir(parents=True)
    git(out, "clone", "--quiet", "--bare", str(source), str(bare))
    return {
        branch: git(bare, "rev-parse", branch)
        for branch in ("main", "ledger-backfill", "ledger-backfill-direct")
    }


@contextlib.contextmanager
def removing_environments(scope: str) -> Iterator[None]:
    """On the way out, remove every environment the demo's runs still hold in ``scope``."""
    try:
        yield
    finally:
        backend = LocalDockerBackend(scope=scope, client=docker.from_env())
        backend.reap_expired(datetime.max.replace(tzinfo=UTC))


@contextlib.contextmanager
def control_database() -> Iterator[str]:
    subprocess.run(["docker", "rm", "-f", CONTROL], capture_output=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            CONTROL,
            "-p",
            f"127.0.0.1:{CONTROL_PORT}:5432",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "postgres:17-alpine",
        ],
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql://postgres@127.0.0.1:{CONTROL_PORT}/postgres"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                psycopg.connect(dsn, connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", CONTROL], capture_output=True)  # noqa: S603, S607


class LiveGitHub(FakeGitHubServer):
    """The test double on a real clock: tests freeze it, but a rehearsal runs for minutes."""

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.fake.now = datetime.now(UTC)
        await super().__call__(scope, receive, send)


@contextlib.contextmanager
def serving(app: Any, port: int) -> Iterator[None]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def deployment(out: Path, image: str) -> Path:
    key = out / "github-app.pem"
    key.write_bytes(PRIVATE_KEY_PEM)
    github = f"http://127.0.0.1:{GITHUB_PORT}"
    path = out / "deployment.toml"
    path.write_text(
        f"""# The local rehearsal: GitHub is the test double on {github}.
[github]
app_id = "{APP_ID}"
private_key_file = "{key.as_posix()}"
client_id = "{CLIENT_ID}"
api_url = "{github}"
web_url = "{github}"
git_url = "{(out / "git").resolve().as_uri()}"

[dashboard]
origin = "{DASHBOARD}"

[previews]
domain = "previews.localhost"
scheme = "http"
port = 8080

[worker]
data_dir = "{(out / "data").as_posix()}"
scope = "{SCOPE}"

[[repositories]]
name = "{REPOSITORY}"
installation_id = {INSTALLATION}

[repositories.project]
runtime_image = "{image}"
baseline = "commerce-v1"
""",
        "utf-8",
    )
    return path


def wait_for(url: str, seconds: float = 120) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx2.HTTPError):
            if httpx2.get(url, timeout=5).status_code < 500:
                return
        time.sleep(1)
    raise SystemExit(f"{url} did not come up")


def deliver(secret: str, base: str, head: str, delivery: str) -> UUID:
    event = {
        "action": "opened",
        "number": 12,
        "pull_request": {
            "number": 12,
            "title": "Move legacy payment events into a per-order ledger",
            "user": {"login": "developer"},
            "base": {"sha": base, "ref": "main", "repo": {"full_name": REPOSITORY}},
            "head": {"sha": head, "ref": "ledger-backfill", "repo": {"full_name": REPOSITORY}},
        },
        "repository": {"full_name": REPOSITORY},
        "installation": {"id": INSTALLATION},
        "sender": {"login": "developer"},
    }
    body = json.dumps(event).encode()
    response = httpx2.post(
        WEBHOOK,
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign(secret.encode(), body),
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if response.status_code != 202:
        raise SystemExit(f"the webhook was refused: {response.status_code} {response.text}")
    return UUID(response.json()["run_id"])


def settled(dsn: str, run_id: UUID, timeout: float = 900) -> tuple[str, float]:
    """Wait for a run's report; its verdict and how long the wait took."""
    store, results, started = RunStore(dsn), RunResults(dsn), time.monotonic()
    while time.monotonic() - started < timeout:
        if store.get_run(run_id).state in SETTLED:
            report = results.report(run_id)
            verdict = report.verdict.value if report else "no report"
            return verdict, time.monotonic() - started
        time.sleep(2)
    raise SystemExit(f"run {run_id} did not finish in {timeout} s")


def expect(dsn: str, run_id: UUID, verdict: str, name: str) -> float:
    """Wait for a run's report, require its verdict; how long the wait took."""
    found, waited = settled(dsn, run_id)
    print(f"{name} {run_id}: {found} after {waited:.0f} s")
    if found != verdict:
        raise SystemExit(f"expected {verdict}; see the worker log under the logs directory")
    return waited


def release(dsn: str, repository: str, keep: UUID, actor: str = "rehearsal") -> None:
    """Cancel, off camera, every other run the walk-through has finished with.

    A run in review keeps its databases and previews (about six Docker networks) until its
    review expires, and one machine's Docker address pool holds only a few runs. Cancelling
    releases them; their reports and decisions stand.
    """
    store = RunStore(dsn)
    for run in store.runs([repository]):
        if run.run_id != keep and store.cancel(run.run_id, actor=actor, reason="released"):
            print(f"released run {run.run_id} ({run.state.value})")


def child(dsn: str, repository: str, parent: UUID, timeout: float = 60) -> UUID:
    """The run a decision on ``parent`` started: here, its correction replay.

    Approval runs are not found this way: several approvals start several runs in quick
    succession, so the walk-through takes the last one's ID from the dashboard instead.
    """
    store, started = RunStore(dsn), time.monotonic()
    while time.monotonic() - started < timeout:
        children = [
            r
            for r in store.runs([repository])
            if r.parent_run_id == parent and r.superseded_by is None
        ]
        if children:
            return max(children, key=lambda r: r.created_at).run_id
        time.sleep(1)
    raise SystemExit(f"no run followed {parent}")


def _writable(function: Any, path: str, _: BaseException) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True, help="the runtime image ID (sha256:...)")
    parser.add_argument("--record", action="store_true", help="record the walk-through")
    parser.add_argument("--out", type=Path, default=REPO / ".dataproof-demo" / "rehearsal")
    args = parser.parse_args(argv)
    if args.out.exists():
        shutil.rmtree(args.out, onexc=_writable)  # git leaves its objects read-only
    args.out.mkdir(parents=True)
    commits = demo_repository(args.out)
    github = FakeGitHub(
        {INSTALLATION: {REPOSITORY}},
        permissions={REPOSITORY: {"maintainer": "admin", "developer": "write"}},
    )
    github.signing_in = "maintainer"
    github.commits = set(commits.values())
    secret = secrets.token_hex(16)
    env_file = args.out / "secrets.env"

    with control_database() as dsn, serving(LiveGitHub(github), GITHUB_PORT):
        env_file.write_text(
            "\n".join(
                [
                    f"DATAPROOF_CONFIG={deployment(args.out, args.image)}",
                    f"DATAPROOF_DATABASE_URL={dsn}",
                    f"DATAPROOF_WEBHOOK_SECRET={secret}",
                    f"DATAPROOF_SESSION_SECRET={secrets.token_hex(24)}",
                    f"DATAPROOF_PREVIEW_SECRET={secrets.token_hex(24)}",
                    f"DATAPROOF_GITHUB_CLIENT_SECRET={CLIENT_SECRET}",
                ]
            )
            + "\n",
            "utf-8",
        )
        # The stack stops first, then what its runs still hold is removed.
        with removing_environments(SCOPE), running(environment(env_file), args.out / "logs"):
            wait_for(f"{DASHBOARD}/", seconds=120)
            wait_for("http://127.0.0.1:8000/openapi.json")
            walk = Walkthrough(args.out / "video" if args.record else None, SIZE)
            try:
                first = deliver(secret, commits["main"], commits["ledger-backfill"], "rehearsal-1")
                approval = walk.binding_review(first, expect(dsn, first, "INCOMPLETE", "run"))
                release(dsn, REPOSITORY, keep=approval)
                waited = expect(dsn, approval, "BLOCKED", "approval run")
                walk.blocked(approval, waited, commits["ledger-backfill-direct"])
                correction = child(dsn, REPOSITORY, approval)
                release(dsn, REPOSITORY, keep=correction)
                waited = expect(dsn, correction, "INCOMPLETE", "correction run")
                corrected = walk.correction_review(correction, waited)
                release(dsn, REPOSITORY, keep=corrected)
                walk.checked(corrected, expect(dsn, corrected, "CHECKED", "corrected run"))
            finally:
                walk.close()
            time.sleep(10)  # the publisher's next pass
            for sha in (commits["ledger-backfill"], commits["ledger-backfill-direct"]):
                for run in github.runs_on(sha):
                    print(f"GitHub check run on {sha[:12]}: {run.status} {run.conclusion}")
            if args.record:
                print("video:", walk.join(args.out / "rehearsal.mp4"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
