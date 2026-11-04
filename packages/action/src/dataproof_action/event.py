"""The GitHub Actions event the Action runs for: a ``pull_request`` event, and nothing else.

``pull_request_target`` is refused outright. It runs with a write-capable token and the
repository's secrets, and executing a pull request's migration under it would put both within
reach of the pull request's code; the isolation here is a second line, not a reason to accept
that.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{40}$")


class EventError(ValueError):
    """The Action was not started by a ``pull_request`` event it can use."""


@dataclass(frozen=True)
class PullRequestEvent:
    repository: str
    """``owner/name`` of the repository the workflow runs in (the base repository)."""
    number: int
    base_ref: str
    base_sha: str
    """The base branch's tip as the event recorded it. The Action verifies against the base
    branch's tip at run time, which may be newer, and records both."""
    head_sha: str
    head_repository: str
    from_fork: bool
    server_url: str
    run_id: str

    @property
    def url(self) -> str:
        return f"{self.server_url}/{self.repository}/pull/{self.number}"


def _sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SHA.match(value):
        raise EventError(f"the event's {what} is not a full commit SHA: {value!r}")
    return value


def load_event(environ: Mapping[str, str]) -> PullRequestEvent:
    name = environ.get("GITHUB_EVENT_NAME", "")
    if name == "pull_request_target":
        raise EventError(
            "DataProof refuses to run on pull_request_target: it would execute the pull "
            "request's migration with a write-capable token and the repository's secrets in "
            "reach. Trigger the workflow on pull_request with permissions: contents: read."
        )
    if name != "pull_request":
        raise EventError(f"DataProof runs on pull_request events only, not {name or 'none'!r}")
    path = environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise EventError("GITHUB_EVENT_PATH is not set")
    try:
        payload = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise EventError(f"the event payload could not be read: {error}") from error
    pull_request = payload.get("pull_request") or {}
    base, head = pull_request.get("base") or {}, pull_request.get("head") or {}
    repository = str((payload.get("repository") or {}).get("full_name") or "")
    head_repository = str((head.get("repo") or {}).get("full_name") or "")
    if not repository or not head_repository:
        raise EventError("the event names no repository")
    number = pull_request.get("number")
    if not isinstance(number, int):
        raise EventError("the event has no pull request number")
    base_ref = str(base.get("ref") or "")
    if not re.match(r"^[A-Za-z0-9._/-]{1,255}$", base_ref) or ".." in base_ref:
        raise EventError(f"the event's base ref is not usable: {base_ref!r}")
    return PullRequestEvent(
        repository=repository,
        number=number,
        base_ref=base_ref,
        base_sha=_sha(base.get("sha"), "base SHA"),
        head_sha=_sha(head.get("sha"), "head SHA"),
        head_repository=head_repository,
        from_fork=bool(head.get("repo", {}).get("fork")) or head_repository != repository,
        server_url=environ.get("GITHUB_SERVER_URL", "https://github.com"),
        run_id=environ.get("GITHUB_RUN_ID", ""),
    )
