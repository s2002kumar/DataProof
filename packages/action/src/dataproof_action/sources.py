"""Trusted reads of exact commits from the job's checkout, on the runner, never in a container.

The Action reads the base branch's tip for everything it trusts (configuration, fixture,
oracles, requirements) and the pull request's head commit for what it proposes (migrations,
bindings), straight from git objects. Nothing is checked out: the working tree, whatever
``actions/checkout`` left there, is not consulted. The migration containers receive only a
``git archive`` of one commit and its digest; they never see the repository, its ``.git``
directory or any credential it may hold.
"""

import subprocess
from pathlib import Path, PurePosixPath

from dataproof_runner.backend import StagedSource
from dataproof_runner.source import archive_commit, changed_paths, merge_base, pull_request_paths

#: How much history of the base and head to fetch, in turn, until their merge base is found. A
#: pull request more than the last step behind or ahead of its base has its changed paths
#: counted from the base's tip.
DEEPEN_STEPS = (50, 500, 5000)


class SourceError(RuntimeError):
    """A commit the Action needs is not available."""


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - fixed git binary, validated arguments
            ["git", "-C", str(self.path), *arguments],  # noqa: S607
            capture_output=True,
            check=check,
        )

    def has_commit(self, sha: str) -> bool:
        return self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def ensure_commit(self, sha: str) -> None:
        """Fetch a commit by SHA if the checkout lacks it (GitHub serves reachable commits)."""
        if self.has_commit(sha):
            return
        fetched = self._git("fetch", "--no-tags", "--depth=1", "origin", sha, check=False)
        if fetched.returncode != 0 or not self.has_commit(sha):
            raise SourceError(
                f"commit {sha} is not in the checkout and could not be fetched: "
                f"{fetched.stderr.decode(errors='replace').strip()[-300:]}"
            )

    def resolve_base(self, base_ref: str) -> str:
        """The base branch's tip now, fetched fresh: the trusted inputs come from it.

        Re-running a job after a maintainer approves a binding on the base branch must see that
        approval, and the event's recorded base SHA would not.
        """
        fetched = self._git(
            "fetch", "--no-tags", "--depth=1", "origin", f"refs/heads/{base_ref}", check=False
        )
        if fetched.returncode != 0:
            raise SourceError(
                f"the base branch {base_ref!r} could not be fetched: "
                f"{fetched.stderr.decode(errors='replace').strip()[-300:]}"
            )
        return self._git("rev-parse", "FETCH_HEAD").stdout.decode().strip()

    def read_text(self, sha: str, path: str) -> str | None:
        """A file's text at a commit, or None if the commit has no such file."""
        shown = self._git("show", f"{sha}:{path}", check=False)
        if shown.returncode != 0:
            return None
        return shown.stdout.decode("utf-8")

    def list_files(self, sha: str, directory: str) -> list[str]:
        """Paths of the regular files directly in a directory at a commit (none if absent).

        ``-z`` gives each name exactly, so a name with a newline or a quote is neither split
        nor quoted; symbolic links, submodules and directories are left out.
        """
        listed = self._git("ls-tree", "-z", f"{sha}:{directory}", check=False)
        if listed.returncode != 0:
            return []
        prefix = PurePosixPath(directory)
        names = []
        for entry in listed.stdout.decode("utf-8").split("\0"):
            if not entry:
                continue
            meta, _, name = entry.partition("\t")
            mode, kind = meta.split(" ")[:2]
            if kind == "blob" and mode in ("100644", "100755"):
                names.append(str(prefix / name))
        return sorted(names)

    def archive(self, sha: str) -> StagedSource:
        return archive_commit(self.path, sha)

    def changed_paths(self, base_sha: str, head_sha: str) -> list[str]:
        return changed_paths(self.path, base_sha, head_sha)

    def is_shallow(self) -> bool:
        shallow = self._git("rev-parse", "--is-shallow-repository", check=False)
        return shallow.stdout.decode().strip() != "false"

    def pull_request_paths(self, base_sha: str, head_sha: str) -> tuple[list[str], str | None]:
        """The paths the pull request changes since its merge base, and that merge base.

        The checkout and the Action's own fetches are shallow, so the merge base is usually
        missing at first: fetch more history of both commits, in widening steps, until it is
        found or the history is complete (``DEEPEN_STEPS``). Without it, the paths are counted
        from the base's tip (``pull_request_paths`` in ``dataproof_runner.source``).
        """
        for depth in (None, *DEEPEN_STEPS):
            if depth is not None:
                if not self.is_shallow():
                    break
                self._git(
                    "fetch",
                    "--no-tags",
                    f"--depth={depth}",
                    "origin",
                    base_sha,
                    head_sha,
                    check=False,
                )
            if merge_base(self.path, base_sha, head_sha) is not None:
                break
        return pull_request_paths(self.path, base_sha, head_sha)
