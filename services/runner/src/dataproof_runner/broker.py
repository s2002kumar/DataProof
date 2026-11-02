"""The trusted source broker: makes the exact commits a run pins available to ``FETCHING``.

Before ``FETCHING`` archives a run's base and head commits, the broker fetches them from the
repository's Git remote into the trusted local mirror, **by SHA**: a branch moved after the
event cannot substitute other code. Fetched commits are pinned under ``refs/dataproof/`` so
garbage collection keeps them.

A credential (a GitHub installation token with ``contents: read``) reaches git through the
environment of that one command, as an ``http.extraHeader``. It is never written to the
mirror's configuration, put on a command line, or included in an error. Credential helpers are
disabled, so nothing caches it either. Steps never see the mirror or the credential: they
receive only the digest-verified archive.
"""

import base64
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

_SHA = re.compile(r"^[0-9a-f]{40}$")
#: Inherited variables that could redirect or reconfigure git are dropped.
_DROPPED_PREFIXES = ("GIT_",)


class SourceFetchError(RuntimeError):
    """The pinned commits could not be fetched. Retried by the worker like any failed attempt."""


class SourceFetcher(Protocol):
    def ensure(self, repository: str, commits: Sequence[str]) -> None:
        """Make every commit present in the mirror, or raise :class:`SourceFetchError`."""


@dataclass(frozen=True)
class GitRemote:
    url: str
    token: str | None = None
    """Sent as HTTP basic auth for ``x-access-token`` (GitHub's form for installation tokens)."""


class GitFetcher:
    def __init__(
        self,
        mirror: Path,
        remote: Callable[[str], GitRemote],
        *,
        timeout: timedelta = timedelta(minutes=5),
    ) -> None:
        self.mirror = mirror
        self.remote = remote
        """The remote of a repository (``owner/name``), with a fresh credential if needed."""
        self.timeout = timeout

    def ensure(self, repository: str, commits: Sequence[str]) -> None:
        for sha in commits:
            if not _SHA.match(sha):
                raise ValueError(f"not a full commit SHA: {sha!r}")
        missing = [sha for sha in dict.fromkeys(commits) if not self.has(sha)]
        if not missing:
            return
        remote = self.remote(repository)
        try:
            fetched = self._git(
                [
                    "-c",
                    "credential.helper=",
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--quiet",
                    remote.url,
                    *missing,
                ],
                environment=self.environment(remote.token),
            )
        except subprocess.TimeoutExpired:
            raise SourceFetchError(f"fetching {repository} timed out") from None
        absent = [sha for sha in missing if not self.has(sha)]
        if fetched.returncode != 0 or absent:
            raise SourceFetchError(
                f"could not fetch {', '.join(absent or missing)} for {repository} "
                f"(git exit {fetched.returncode})"
            )
        for sha in missing:
            self._git(["update-ref", f"refs/dataproof/commits/{sha}", sha], check=True)

    def has(self, sha: str) -> bool:
        return self._git(["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0

    @staticmethod
    def environment(token: str | None) -> dict[str, str]:
        """The environment for one git command: inherited ``GIT_*`` settings removed, prompts
        disabled, and the credential, if any, as a header that exists only in this process."""
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.upper().startswith(_DROPPED_PREFIXES)
        }
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if token is not None:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            environment |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        return environment

    def _git(
        self,
        arguments: list[str],
        *,
        environment: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - fixed git binary, validated arguments
            ["git", "-C", str(self.mirror), *arguments],  # noqa: S607
            capture_output=True,
            check=check,
            env=dict(environment) if environment is not None else self.environment(None),
            timeout=self.timeout.total_seconds(),
        )
