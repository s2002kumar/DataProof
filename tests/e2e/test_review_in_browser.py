"""A reviewer signs in and approves a binding in the dashboard, in a real browser.

The stack is what a deployment runs, with GitHub faked over HTTP:

* the control plane API, composed by ``Stack`` from a deployment file and environment secrets;
* the dashboard, built and served by Next.js, with ``/api`` and ``/auth`` routed to the API;
* a fake GitHub that signs the reviewer in, issues App tokens and answers permission checks;
* PostgreSQL.

A signed webhook creates the run. Its check waiting on binding review is recorded the way
``VERIFYING`` records it. Nothing else is set up by hand: the reviewer does the rest in the
browser.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import psycopg
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from control_env import DATASET_DIGEST, HEAD_SHA
from dataproof_api import sign
from dataproof_api.deployment import Stack
from dataproof_contracts import (
    BindingAnalysis,
    BindingRelation,
    CandidateBinding,
    CheckOutcome,
    CheckResult,
    HumanDecisionAction,
    NotRunReason,
    RelationProvenance,
    RunTrigger,
)
from dataproof_control import CheckRecord, RunStore
from dataproof_control.results import record_check
from github_env import (
    APP_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    INSTALLATION,
    PRIVATE_KEY_PEM,
    REPOSITORY,
    FakeGitHub,
    FakeGitHubServer,
    pull_request_event,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    # Needs pnpm, the dashboard's dependencies and Chromium; CI's e2e job provides them.
    pytest.mark.skipif(
        os.environ.get("DATAPROOF_E2E") != "1",
        reason="set DATAPROOF_E2E=1 to drive the dashboard in a browser",
    ),
]

ROOT = Path(__file__).parents[2]
DASHBOARD = ROOT / "services" / "dashboard"
# Next.js fixes its rewrites when it builds, so the API listens on a known port.
API_PORT = 18765
DASHBOARD_PORT = 13765
DASHBOARD_ORIGIN = f"http://127.0.0.1:{DASHBOARD_PORT}"
WEBHOOK_SECRET = "e2e-webhook-secret-0123456789"  # noqa: S105 - a test placeholder


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serving(app: Any, port: int) -> Iterator[None]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError(f"nothing is serving on port {port}")
        time.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def dashboard() -> Iterator[str]:
    """The dashboard, built against the test API's address and served by ``next start``."""
    environment = {
        **os.environ,
        "DATAPROOF_API_ORIGIN": f"http://127.0.0.1:{API_PORT}",
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    pnpm = shutil.which("pnpm")
    node = shutil.which("node")
    assert pnpm is not None, "pnpm is needed to build the dashboard"
    assert node is not None
    subprocess.run(
        [pnpm, "--filter", "@dataproof/dashboard", "build"], cwd=ROOT, env=environment, check=True
    )
    process = subprocess.Popen(
        [
            node,
            "node_modules/next/dist/bin/next",
            "start",
            "-H",
            "127.0.0.1",
            "-p",
            str(DASHBOARD_PORT),
        ],
        cwd=DASHBOARD,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{DASHBOARD_ORIGIN}/", timeout=5) as page:
                    if page.status == 200:
                        break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield DASHBOARD_ORIGIN
    finally:
        process.terminate()
        process.wait(timeout=20)


def deployment_file(tmp_path: Path, github_url: str) -> Path:
    key = tmp_path / "app.pem"
    key.write_bytes(PRIVATE_KEY_PEM)
    path = tmp_path / "deployment.toml"
    path.write_text(
        f"""
[github]
app_id = "{APP_ID}"
private_key_file = "{key.as_posix()}"
client_id = "{CLIENT_ID}"
api_url = "{github_url}"
web_url = "{github_url}"

[dashboard]
origin = "{DASHBOARD_ORIGIN}"

[previews]
domain = "previews.test"
scheme = "http"

[worker]
data_dir = "{(tmp_path / "data").as_posix()}"

[[repositories]]
name = "{REPOSITORY}"
installation_id = {INSTALLATION}

[repositories.project]
runtime_image = "sha256:{"0" * 64}"
baseline = "commerce-v1"
""",
        "utf-8",
    )
    return path


def awaiting_review(dsn: str, run_id: UUID) -> CandidateBinding:
    binding = CandidateBinding(
        source_path="dataproof/bindings/c2_refund_totals.toml",
        head_sha=HEAD_SHA,
        query_sql="SELECT order_id, sum(amount) FROM ledger_entries JOIN orders USING (order_id)",
        column_mapping={"order_id": "order_id", "refund_total": "sum"},
    )
    analysis = BindingAnalysis(
        binding_digest=binding.digest(),
        relations=(
            BindingRelation(name="public.ledger_entries", provenance=RelationProvenance.CREATED),
            BindingRelation(name="public.orders", provenance=RelationProvenance.UNCHANGED),
        ),
        functions=("sum(numeric)",),
    )
    result = CheckResult(
        check_id="commerce.c2_refund_totals",
        contract_id="commerce.c2_refund_totals",
        contract_version=1,
        required=True,
        outcome=CheckOutcome.NOT_RUN,
        not_run_reason=NotRunReason.AWAITING_BINDING_REVIEW,
        dataset_digest=DATASET_DIGEST,
    )
    with psycopg.connect(dsn) as conn:
        record_check(conn, run_id, CheckRecord(result, binding=binding, binding_analysis=analysis))
    return binding


def test_a_reviewer_signs_in_and_approves_a_binding_in_the_browser(
    control_dsn: str, dashboard: str, tmp_path: Path
) -> None:
    github = FakeGitHub(
        {INSTALLATION: {REPOSITORY}},
        permissions={REPOSITORY: {"maintainer": "write", "developer": "write"}},
    )
    github.signing_in = "maintainer"
    github_port = free_port()
    environment = {
        "DATAPROOF_CONFIG": str(deployment_file(tmp_path, f"http://127.0.0.1:{github_port}")),
        "DATAPROOF_DATABASE_URL": control_dsn,
        "DATAPROOF_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "DATAPROOF_SESSION_SECRET": "e2e-session-secret-0123456789abcdef",
        "DATAPROOF_PREVIEW_SECRET": "e2e-preview-secret-0123456789abcdef",
        "DATAPROOF_GITHUB_CLIENT_SECRET": CLIENT_SECRET,
    }
    stack = Stack.from_environment(environment)

    with serving(FakeGitHubServer(github), github_port), serving(stack.api(), API_PORT):
        body = json.dumps(pull_request_event()).encode()
        delivered = httpx2.post(
            f"http://127.0.0.1:{API_PORT}/github/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "e2e-1",
                "X-Hub-Signature-256": sign(WEBHOOK_SECRET.encode(), body),
                "Content-Type": "application/json",
            },
        )
        assert delivered.status_code == 202, delivered.text
        run_id = UUID(delivered.json()["run_id"])
        binding = awaiting_review(control_dsn, run_id)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f"{dashboard}/")
            page.get_by_role("link", name="Sign in with GitHub").click()
            expect(page.get_by_text("Signed in as maintainer")).to_be_visible()

            page.get_by_role("link", name=str(run_id)[:8]).click()
            expect(page.locator("pre.sql")).to_have_text(str(binding.query_sql))
            expect(page.get_by_role("note")).to_contain_text("public.orders")
            page.get_by_label("Comment (optional)").fill("Reads the new ledger.")
            page.get_by_role("button", name="Approve binding").click()
            expect(page.get_by_role("status")).to_contain_text("A new run started")
            expect(page.locator("li", has_text="APPROVE_BINDING")).to_contain_text(
                "maintainer APPROVE_BINDING"
            )
            browser.close()

    store = RunStore(control_dsn)
    (decision,) = store.decisions(run_id)
    assert decision.action is HumanDecisionAction.APPROVE_BINDING
    assert (decision.actor, decision.pr_author) == ("maintainer", "developer")
    assert decision.comment == "Reads the new ledger."
    assert decision.binding_digest == binding.digest()
    children = [run for run in store.runs([REPOSITORY]) if run.parent_run_id == run_id]
    assert [run.trigger for run in children] == [RunTrigger.BINDING_APPROVAL]
