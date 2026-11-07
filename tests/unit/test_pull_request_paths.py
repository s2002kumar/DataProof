"""A pull request's changed paths are counted from its merge base, as GitHub's diff is.

A branch that is behind its base must not appear to change what the base changed after it
branched: the oracles, the configuration or the dependencies. Needs only git.
"""

import subprocess
from pathlib import Path

import pytest

from dataproof_action.sources import DEEPEN_STEPS, Repository
from dataproof_runner.source import changed_paths, merge_base, pull_request_paths


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=DataProof Tests",
            "-c",
            "user.email=tests@dataproof.invalid",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(repository),
            *arguments,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def commit(repository: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "--allow-empty", "-m", message)
    return git(repository, "rev-parse", "HEAD")


class Stale:
    """main gains a trusted-file change after the pull request's branch was cut."""

    def __init__(self, root: Path, *, commits_on_main: int = 1) -> None:
        self.path = root / "upstream"
        self.path.mkdir()
        git(self.path, "init", "-q", "-b", "main")
        git(self.path, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.branched_from = commit(
            self.path,
            {"dataproof/config.toml": "version = 1\n", "migrations/versions/0001.py": "one\n"},
            "Base",
        )
        git(self.path, "switch", "-q", "-c", "feature")
        self.head = commit(self.path, {"migrations/versions/0002.py": "two\n"}, "Pull request")
        git(self.path, "switch", "-q", "main")
        self.base = commit(
            self.path,
            {"dataproof/oracles/new.toml": "contract\n", "pyproject.toml": "[project]\n"},
            "main gains an oracle and a dependency change",
        )
        for number in range(commits_on_main - 1):
            self.base = commit(self.path, {}, f"main moves on ({number})")


def test_a_stale_branch_changes_only_its_own_paths(tmp_path: Path) -> None:
    stale = Stale(tmp_path)
    assert merge_base(stale.path, stale.base, stale.head) == stale.branched_from
    assert pull_request_paths(stale.path, stale.base, stale.head) == (
        ["migrations/versions/0002.py"],
        stale.branched_from,
    )
    assert changed_paths(stale.path, stale.base, stale.head) == [
        "dataproof/oracles/new.toml",
        "migrations/versions/0002.py",
        "pyproject.toml",
    ], "the tip-to-head diff is what counted before, and it names the base's own changes"


def test_unrelated_histories_count_every_difference(tmp_path: Path) -> None:
    stale = Stale(tmp_path)
    git(stale.path, "switch", "-q", "--orphan", "unrelated")
    other = commit(stale.path, {"other.txt": "x\n"}, "Unrelated")
    assert merge_base(stale.path, stale.base, other) is None
    paths, since = pull_request_paths(stale.path, stale.base, other)
    assert since is None
    assert paths == changed_paths(stale.path, stale.base, other)


@pytest.mark.parametrize("commits_on_main", [1, DEEPEN_STEPS[0] + 10])
def test_the_action_deepens_a_shallow_checkout_until_it_finds_the_merge_base(
    tmp_path: Path, commits_on_main: int
) -> None:
    stale = Stale(tmp_path, commits_on_main=commits_on_main)
    workspace = tmp_path / "workspace"
    git(
        tmp_path,
        "clone",
        "-q",
        "--depth=1",
        "--branch",
        "main",
        stale.path.as_uri(),
        str(workspace),
    )
    repository = Repository(workspace)
    repository.ensure_commit(stale.head)
    assert repository.is_shallow()
    assert merge_base(workspace, stale.base, stale.head) is None, "shallow: not yet visible"
    assert repository.pull_request_paths(stale.base, stale.head) == (
        ["migrations/versions/0002.py"],
        stale.branched_from,
    )


def test_a_full_history_needs_no_fetch(tmp_path: Path) -> None:
    stale = Stale(tmp_path)
    workspace = tmp_path / "workspace"
    git(tmp_path, "clone", "-q", stale.path.as_uri(), str(workspace))
    git(workspace, "fetch", "-q", "origin", "feature")
    repository = Repository(workspace)
    assert not repository.is_shallow()
    git(workspace, "remote", "set-url", "origin", (tmp_path / "gone").as_uri())
    assert repository.pull_request_paths(stale.base, stale.head) == (
        ["migrations/versions/0002.py"],
        stale.branched_from,
    )
