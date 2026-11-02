"""The source broker fetches pinned commits by SHA and never stores its credential (git only)."""

import base64
import subprocess
from pathlib import Path

import pytest

from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_api.sources import github_remotes
from dataproof_runner.broker import GitFetcher, GitRemote, SourceFetchError
from github_env import APP_ID, INSTALLATION, PRIVATE_KEY_PEM, REPOSITORY, FakeGitHub

TOKEN = "test-only-installation-token"  # noqa: S105 - a placeholder, not a credential


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def commit(repository: Path, name: str, text: str) -> str:
    (repository / name).write_text(text, "utf-8")
    git(repository, "add", name)
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        f"Add {name}",
    )
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    repository = tmp_path / "remote"
    repository.mkdir()
    git(repository, "init", "--quiet", "--initial-branch=main")
    # GitHub serves any commit reachable from a ref (including refs/pull/*) by SHA.
    git(repository, "config", "uploadpack.allowAnySHA1InWant", "true")
    return repository


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    repository = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(repository)], check=True)
    return repository


def fetcher(mirror: Path, remote: Path, token: str | None = TOKEN) -> GitFetcher:
    return GitFetcher(mirror, lambda _: GitRemote(remote.as_uri(), token))


def test_pinned_commits_are_fetched_by_sha_and_kept(remote: Path, mirror: Path) -> None:
    base = commit(remote, "a.txt", "base\n")
    head = commit(remote, "b.txt", "head\n")
    commit(remote, "c.txt", "pushed after the event\n")
    broker = fetcher(mirror, remote)

    broker.ensure(REPOSITORY, [base, head])

    assert broker.has(base)
    assert broker.has(head)
    assert git(mirror, "rev-parse", f"refs/dataproof/commits/{head}") == head
    assert git(mirror, "show", f"{head}:b.txt") == "head"
    # Already present: nothing is fetched, so even an unreachable remote does not matter.
    GitFetcher(mirror, lambda _: GitRemote("file:///nowhere")).ensure(REPOSITORY, [head])


def test_the_credential_is_never_written_to_the_mirror(remote: Path, mirror: Path) -> None:
    head = commit(remote, "a.txt", "content\n")
    fetcher(mirror, remote).ensure(REPOSITORY, [head])
    for path in mirror.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert TOKEN.encode() not in data, path
            assert b"extraheader" not in data.lower(), path
    assert "extraheader" not in git(mirror, "config", "--list").lower()


def test_git_sees_the_credential_as_a_header_for_that_command_only(mirror: Path) -> None:
    environment = GitFetcher.environment(TOKEN)
    header = subprocess.run(
        ["git", "-C", str(mirror), "config", "--get", "http.extraHeader"],
        capture_output=True,
        check=True,
        text=True,
        env=environment,
    ).stdout.strip()
    scheme, value = header.removeprefix("Authorization: ").split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(value).decode() == f"x-access-token:{TOKEN}"
    assert GitFetcher.environment(None).get("GIT_CONFIG_COUNT") is None


def test_inherited_git_settings_cannot_redirect_the_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    environment = GitFetcher.environment(None)
    assert "GIT_DIR" not in environment
    assert "GIT_CONFIG_COUNT" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_a_commit_the_remote_does_not_have_is_an_error_without_the_credential(
    remote: Path, mirror: Path
) -> None:
    commit(remote, "a.txt", "content\n")
    missing = "0" * 40
    with pytest.raises(SourceFetchError, match=missing) as raised:
        fetcher(mirror, remote).ensure(REPOSITORY, [missing])
    assert TOKEN not in str(raised.value)


def test_only_full_shas_are_fetched(remote: Path, mirror: Path) -> None:
    with pytest.raises(ValueError, match="full commit SHA"):
        fetcher(mirror, remote).ensure(REPOSITORY, ["main"])


def test_github_remotes_use_a_fresh_token_for_that_repository_alone() -> None:
    github = FakeGitHub({INSTALLATION: {REPOSITORY, "example-org/other"}})
    app = GitHubApp(APP_ID, load_private_key(PRIVATE_KEY_PEM), client=github.client())
    remote = github_remotes(app, {REPOSITORY: INSTALLATION})(REPOSITORY)
    assert remote.url == f"https://github.com/{REPOSITORY}.git"
    assert remote.token is not None
    _, repositories, _, permissions = github.tokens[remote.token]
    assert repositories == {REPOSITORY}
    assert permissions["contents"] == "read"
    with pytest.raises(LookupError):
        github_remotes(app, {REPOSITORY: INSTALLATION})("example-org/other")
