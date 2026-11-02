"""Readiness for a real GitHub App: least privilege, one public route, safe credential handling."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_deployment import SECRETS, _deployment

from dataproof_api.deployment import Stack
from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_api.registration import Registration, app_manifest
from dataproof_runner.project import ProjectConfig
from github_env import APP_ID, PRIVATE_KEY_PEM, FakeGitHub

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "demo"))
import build_demo_repo  # noqa: E402

MANIFEST = app_manifest(
    name="DataProof dev",
    webhook_url="https://example.trycloudflare.com/github/webhook",
    dashboard_origin="http://127.0.0.1:3000",
    redirect_url="http://127.0.0.1:8765/callback",
    homepage="https://github.com/s2002kumar/DataProof",
)
CONVERSION = {
    "id": 4242,
    "slug": "dataproof-dev",
    "client_id": "Iv1.registered",
    "client_secret": "test-only-client-secret-from-github",
    "webhook_secret": "test-only-webhook-secret-from-github",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\ntest-only\n-----END RSA PRIVATE KEY-----\n",
    "html_url": "https://github.com/apps/dataproof-dev",
}


def test_the_app_asks_for_the_least_it_needs() -> None:
    assert MANIFEST["default_permissions"] == {
        "checks": "write",
        "contents": "read",
        "metadata": "read",
        "pull_requests": "read",
    }
    assert MANIFEST["default_events"] == ["pull_request"]
    assert MANIFEST["public"] is False
    assert MANIFEST["callback_urls"] == ["http://127.0.0.1:3000/auth/callback"]
    assert MANIFEST["hook_attributes"]["active"] is True


def test_the_public_process_serves_the_webhook_and_nothing_else(tmp_path: Path) -> None:
    stack = Stack.from_environment({**SECRETS, "DATAPROOF_CONFIG": str(_deployment(tmp_path))})
    app = stack.webhook()
    assert {getattr(route, "path", None) for route in app.routes} == {"/github/webhook"}
    client = TestClient(app)
    for path in ("/openapi.json", "/docs", "/redoc", "/api/session", "/auth/login"):
        assert client.get(path).status_code == 404, path
    assert client.post("/github/webhook", content=b"{}").status_code == 422, "headers required"


def test_registration_stores_the_credentials_and_never_shows_them(tmp_path: Path) -> None:
    github = FakeGitHub({}, conversions={"code-1": dict(CONVERSION)})
    registration = Registration(MANIFEST, tmp_path / "secrets", owner=None, api=github.client())
    browser = TestClient(registration.app())

    page = browser.get("/").text
    assert f"https://github.com/settings/apps/new?state={registration.state}" in page
    assert "&quot;checks&quot;: &quot;write&quot;" in page
    assert browser.get("/callback", params={"code": "code-1", "state": "forged"}).status_code == 400

    done = browser.get("/callback", params={"code": "code-1", "state": registration.state})
    assert done.status_code == 200
    for secret in ("client_secret", "webhook_secret", "pem"):
        assert str(CONVERSION[secret]).strip() not in done.text
    assert "github.com/apps/dataproof-dev/installations/new" in done.text
    saved = tmp_path / "secrets"
    assert (saved / "github-app.pem").read_text("utf-8") == CONVERSION["pem"]
    environment = (saved / "secrets.env").read_text("utf-8")
    assert f"DATAPROOF_WEBHOOK_SECRET={CONVERSION['webhook_secret']}" in environment
    generated = dict(line.split("=", 1) for line in environment.splitlines())
    for name in ("DATAPROOF_SESSION_SECRET", "DATAPROOF_PREVIEW_SECRET"):
        assert len(generated[name]) >= 32, name
        assert generated[name] not in done.text
    assert json.loads((saved / "github-app.json").read_text("utf-8"))["app_id"] == 4242
    if os.name == "posix":
        assert (saved / "github-app.pem").stat().st_mode & 0o077 == 0


def test_the_registered_secrets_are_all_the_stack_needs(tmp_path: Path) -> None:
    """Besides the control database's address: the live setup sets nothing else by hand."""
    github = FakeGitHub({}, conversions={"code-1": dict(CONVERSION)})
    registration = Registration(MANIFEST, tmp_path / "secrets", owner=None, api=github.client())
    TestClient(registration.app()).get(
        "/callback", params={"code": "code-1", "state": registration.state}
    )
    written = (tmp_path / "secrets" / "secrets.env").read_text("utf-8").splitlines()
    environ = dict(line.split("=", 1) for line in written)
    environ["DATAPROOF_DATABASE_URL"] = SECRETS["DATAPROOF_DATABASE_URL"]
    environ["DATAPROOF_CONFIG"] = str(_deployment(tmp_path))
    stack = Stack.from_environment(environ)
    stack.api()
    stack.webhook()
    stack.gateway()


def test_the_webhook_follows_a_new_tunnel_address_as_the_app() -> None:
    github = FakeGitHub({})
    app = GitHubApp(APP_ID, load_private_key(PRIVATE_KEY_PEM), client=github.client())
    app.set_webhook_url("https://next.trycloudflare.com/github/webhook")
    assert github.hook_config == {
        "url": "https://next.trycloudflare.com/github/webhook",
        "content_type": "json",
        "insecure_ssl": "0",
    }


@pytest.mark.parametrize("branch", list(build_demo_repo.BRANCHES))
def test_each_demo_pull_request_changes_only_what_dataproof_executes(
    tmp_path: Path, branch: str
) -> None:
    repository = tmp_path / "demo"
    heads = build_demo_repo.build(repository)
    changed = subprocess.run(
        ["git", "-C", str(repository), "diff", "--name-only", heads["main"], heads[branch]],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    assert any(path.startswith("sample-app/migrations/versions/") for path in changed)
    assert any(path.startswith("dataproof/bindings/") for path in changed)
    config = ProjectConfig(
        repository="owner/demo", runtime_image="sha256:" + "a" * 64, baseline="commerce-v1"
    )
    assert config.policy_violations(changed) == [], "the pull request would be executed"
    main_files = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "--name-only", heads["main"]],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    assert "dataproof/oracles/c1_event_preservation.toml" in main_files
    assert not any(path.startswith("dataproof/bindings/") for path in main_files)
