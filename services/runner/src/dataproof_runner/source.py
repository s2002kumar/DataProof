"""Source staging by the trusted broker: exact-commit archives of a configured repository.

Locally the repository is a trusted clone on disk; the GitHub tarball endpoint replaces it when
the GitHub App arrives. Either way, steps receive only the archive and its digest, never the
repository or any credential.
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
