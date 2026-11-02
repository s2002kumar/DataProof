"""GitHub webhook signatures and pull request event parsing."""

import pytest
from pydantic import ValidationError

from dataproof_api.webhooks import PullRequestEvent, sign, verify_signature

SECRET = b"webhook-secret-for-tests"
BODY = b'{"action":"opened"}'


def test_only_the_exact_body_under_the_shared_secret_verifies() -> None:
    signature = sign(SECRET, BODY)
    assert signature.startswith("sha256=")
    assert verify_signature(SECRET, BODY, signature)
    assert not verify_signature(SECRET, BODY + b" ", signature), "the body was altered"
    assert not verify_signature(b"another-secret", BODY, signature)
    assert not verify_signature(SECRET, BODY, None)
    assert not verify_signature(SECRET, BODY, signature.removeprefix("sha256="))
    assert not verify_signature(SECRET, BODY, "sha1=" + signature.removeprefix("sha256="))
    assert not verify_signature(SECRET, BODY, signature.upper())


def test_an_empty_secret_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="secret is required"):
        verify_signature(b"", BODY, sign(b"", BODY))


def _event(sender: str) -> dict[str, object]:
    ref = {"sha": "8f3ab2a3b2ce14772811945b1f7ea036b769932e", "repo": {"full_name": "o/r"}}
    return {
        "action": "opened",
        "pull_request": {"number": 1, "user": {"login": sender}, "base": ref, "head": ref},
        "repository": {"full_name": "o/r", "private": False},
        "sender": {"login": sender, "type": "Bot"},
    }


def test_events_from_apps_and_people_parse_and_extra_fields_are_ignored() -> None:
    assert PullRequestEvent.model_validate(_event("dependabot[bot]")).sender.login == (
        "dependabot[bot]"
    )
    assert PullRequestEvent.model_validate(_event("octo-cat")).installation is None


@pytest.mark.parametrize("login", ["-leading-dash", "with space", "x" * 40, "a[bot]x"])
def test_malformed_logins_are_rejected(login: str) -> None:
    with pytest.raises(ValidationError):
        PullRequestEvent.model_validate(_event(login))
