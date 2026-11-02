"""GitHub App authentication, checked by a fake GitHub that verifies what GitHub verifies."""

import json
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dataproof_api.github_app import GitHubApp, GitHubError, load_private_key
from github_env import APP_ID, PRIVATE_KEY_PEM, FakeGitHub

REPOSITORY = "example-org/commerce-demo"
INSTALLATION = 4242


class Clock:
    def __init__(self, fake: FakeGitHub) -> None:
        self.fake = fake

    def __call__(self) -> datetime:
        return self.fake.now

    def advance(self, by: timedelta) -> None:
        self.fake.now += by


def _app(fake: FakeGitHub) -> tuple[GitHubApp, Clock]:
    clock = Clock(fake)
    return GitHubApp(
        APP_ID, load_private_key(PRIVATE_KEY_PEM), client=fake.client(), clock=clock
    ), clock


def _token_requests(fake: FakeGitHub) -> list[dict[str, object]]:
    return [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/access_tokens")]


def test_an_installation_token_is_scoped_to_one_repository_and_minimal_permissions() -> None:
    fake = FakeGitHub({INSTALLATION: {REPOSITORY, "example-org/other"}})
    app, _ = _app(fake)
    token = app.installation_token(INSTALLATION, REPOSITORY)
    assert token in fake.tokens
    assert _token_requests(fake) == [
        {
            "repositories": ["commerce-demo"],
            "permissions": {
                "checks": "write",
                "contents": "read",
                "metadata": "read",
                "pull_requests": "read",
            },
        }
    ]


def test_tokens_are_cached_until_shortly_before_they_expire() -> None:
    fake = FakeGitHub({INSTALLATION: {REPOSITORY}})
    app, clock = _app(fake)
    first = app.installation_token(INSTALLATION, REPOSITORY)
    clock.advance(timedelta(minutes=50))
    assert app.installation_token(INSTALLATION, REPOSITORY) == first
    clock.advance(timedelta(minutes=6))
    assert app.installation_token(INSTALLATION, REPOSITORY) != first, "renewed near expiry"
    assert len(_token_requests(fake)) == 2


def test_a_jwt_signed_by_another_key_is_refused() -> None:
    fake = FakeGitHub({INSTALLATION: {REPOSITORY}})
    app, clock = _app(fake)
    other = GitHubApp(
        APP_ID,
        load_private_key(_another_key()),
        client=fake.client(),
        clock=clock,
    )
    with pytest.raises(GitHubError, match="401"):
        other.installation_token(INSTALLATION, REPOSITORY)
    assert app.installation_token(INSTALLATION, REPOSITORY)


def test_a_refusal_never_carries_a_token() -> None:
    fake = FakeGitHub({INSTALLATION: {REPOSITORY}})
    app, _ = _app(fake)
    repository = app.repository(INSTALLATION, REPOSITORY)
    token = app.installation_token(INSTALLATION, REPOSITORY)
    fake.fail_next = [502]
    with pytest.raises(GitHubError) as raised:
        repository.create_check_run({"name": "DataProof", "head_sha": "a" * 40})
    assert raised.value.status == 502
    assert token not in str(raised.value)


def test_an_installation_cannot_be_used_for_a_repository_it_is_not_on() -> None:
    fake = FakeGitHub({INSTALLATION: {REPOSITORY}})
    app, _ = _app(fake)
    with pytest.raises(GitHubError, match="422"):
        app.installation_token(INSTALLATION, "example-org/elsewhere")


def _another_key() -> bytes:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
