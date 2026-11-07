"""Source staging by the trusted broker: exact-commit archives of a configured repository.

The repository is a trusted mirror on disk, which the source broker (``broker``) fills with the
exact commits a run pins before ``FETCHING`` archives them. Steps receive only the archive and
its digest, never the repository or any credential.
"""

import re
import subprocess
from pathlib import Path

from dataproof_runner.backend import StagedSource

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _git(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(  # noqa: S603 - fixed git binary, validated arguments
        ["git", "-C", str(repository), *arguments],  # noqa: S607
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _commit(sha: str) -> str:
    if not _SHA.match(sha):
        raise ValueError(f"not a full commit SHA: {sha!r}")
    return sha


def archive_commit(repository: Path, commit_sha: str) -> StagedSource:
    """A tar archive of exactly ``commit_sha`` (no working-tree or ignored files)."""
    data = _git(repository, "archive", "--format=tar", _commit(commit_sha))
    return StagedSource.of(commit_sha, data)


def changed_paths(repository: Path, base_sha: str, head_sha: str) -> list[str]:
    """Every path added, removed or modified between two commits; a rename lists both paths."""
    output = _git(
        repository,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        _commit(base_sha),
        _commit(head_sha),
    )
    return sorted(path for path in output.decode("utf-8").split("\0") if path)


def merge_base(repository: Path, base_sha: str, head_sha: str) -> str | None:
    """The commit a pull request's head branched from its base, or None if the clone has none.

    None means unrelated histories, or a shallow clone cut off before the common ancestor.
    """
    completed = subprocess.run(  # noqa: S603 - fixed git binary, validated arguments
        ["git", "-C", str(repository), "merge-base", _commit(base_sha), _commit(head_sha)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if completed.returncode == 1:
        return None
    completed.check_returncode()
    found = completed.stdout.decode().strip()
    return found if _SHA.match(found) else None


def pull_request_paths(
    repository: Path, base_sha: str, head_sha: str
) -> tuple[list[str], str | None]:
    """The paths a pull request changes, and the merge base they are counted from.

    GitHub's diff of a pull request compares its head with the merge base, so a change made on
    the base branch after the head branched is not the pull request's. Comparing with the base's
    tip instead would count every such change, and a stale branch would appear to edit the
    oracles, the configuration or the dependencies that its base edited. Without a merge base,
    the paths are those that differ from the base itself: a superset, never fewer.
    """
    since = merge_base(repository, base_sha, head_sha)
    return changed_paths(repository, since or base_sha, head_sha), since
