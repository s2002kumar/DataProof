"""Deployment configuration: a trusted file without secrets, and the processes composed from it."""

from importlib.metadata import version
from pathlib import Path

import pytest
from pydantic import ValidationError

from dataproof_api.deployment import Deployment, MissingSecretError, Secrets, Stack
from dataproof_runner.baselines import baseline_for
from github_env import PRIVATE_KEY_PEM

EXAMPLE = Path(__file__).parents[2] / "services" / "api" / "deployment.example.toml"


def _deployment(tmp_path: Path, replace: dict[str, str] | None = None) -> Path:
    text = EXAMPLE.read_text("utf-8")
    key = tmp_path / "app.pem"
    key.write_bytes(PRIVATE_KEY_PEM)
    text = text.replace("/run/secrets/dataproof-github-app.pem", key.as_posix())
    for old, new in (replace or {}).items():
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "deployment.toml"
    path.write_text(text, "utf-8")
    return path


SECRETS = {
    "DATAPROOF_DATABASE_URL": "postgresql://dataproof@127.0.0.1:5432/dataproof",
    "DATAPROOF_WEBHOOK_SECRET": "w" * 32,
    "DATAPROOF_SESSION_SECRET": "s" * 32,
    "DATAPROOF_PREVIEW_SECRET": "p" * 32,
    "DATAPROOF_GITHUB_CLIENT_SECRET": "test-only-client-secret",
}


def test_the_example_deployment_loads(tmp_path: Path) -> None:
    deployment = Deployment.load(_deployment(tmp_path))
    assert deployment.installations == {"example-org/commerce-demo": 4242}
    (config,) = deployment.configs.values()
    assert config.repository == "example-org/commerce-demo"
    assert deployment.dashboard.origin == "https://dataproof.example"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('client_id = "Iv1.0000000000000000"', 'client_secret = "oops"', "client_secret"),
        ('origin = "https://dataproof.example"', 'origin = "http://dataproof.example"', "https"),
        (
            'baseline = "commerce-v1"',
            'baseline = "commerce-v1"\nwebhook_secret = "oops"',
            "webhook",
        ),
    ],
)
def test_secrets_and_unsafe_settings_are_refused_in_the_file(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Deployment.load(_deployment(tmp_path, {old: new}))


def test_a_loopback_dashboard_may_use_plain_http(tmp_path: Path) -> None:
    replaced = {'origin = "https://dataproof.example"': 'origin = "http://127.0.0.1:3000"'}
    assert Deployment.load(_deployment(tmp_path, replaced)).dashboard.origin == (
        "http://127.0.0.1:3000"
    )


def test_the_api_composes_from_the_file_and_the_environment(tmp_path: Path) -> None:
    stack = Stack.from_environment({**SECRETS, "DATAPROOF_CONFIG": str(_deployment(tmp_path))})
    paths = stack.api().openapi()["paths"]
    assert {"/github/webhook", "/auth/login", "/api/runs/{run_id}/binding-decisions"} <= set(paths)
    assert stack.publisher().installations == {"example-org/commerce-demo": 4242}
    (registration,) = stack.registrations().values()
    assert registration.dataset_digest == baseline_for("commerce-v1").digest()
    assert registration.engine_version == version("dataproof-runner")


@pytest.mark.parametrize(
    ("missing", "short"),
    [
        ("DATAPROOF_SESSION_SECRET", False),
        ("DATAPROOF_WEBHOOK_SECRET", False),
        ("DATAPROOF_GITHUB_CLIENT_SECRET", False),
        ("DATAPROOF_SESSION_SECRET", True),
    ],
)
def test_a_missing_or_weak_secret_names_its_variable(
    tmp_path: Path, missing: str, short: bool
) -> None:
    environ = {**SECRETS, "DATAPROOF_CONFIG": str(_deployment(tmp_path))}
    if short:
        environ[missing] = "too-short"
    else:
        del environ[missing]
    with pytest.raises(MissingSecretError, match=missing):
        Stack.from_environment(environ).api()


def test_secrets_come_only_from_the_environment() -> None:
    with pytest.raises(MissingSecretError, match="DATAPROOF_DATABASE_URL"):
        _ = Secrets({}).database_url
    with pytest.raises(MissingSecretError, match="DATAPROOF_CONFIG"):
        Stack.from_environment({})
