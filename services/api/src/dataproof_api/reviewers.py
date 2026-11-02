"""Who a reviewer is (GitHub sign-in) and what they may do (repository permission).

Sign-in is GitHub's OAuth web flow for the App. The user's GitHub token is used once, to learn
their login, and then discarded: DataProof never acts as the user. What a login may do is read
from the repository itself, with the App's installation token, at the time of the request:
``read`` (or better) to see a repository's runs, ``write`` (or ``admin``) to decide on them.
Answers are cached briefly, so losing access takes effect within ``ttl``.
"""

import re
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx2

from dataproof_api.github_app import API_VERSION, GitHubApp, Repository

_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
READ_LEVELS = frozenset({"admin", "write", "read"})
DECIDE_LEVELS = frozenset({"admin", "write"})


class SignInError(RuntimeError):
    """GitHub did not confirm who is signing in."""


class GitHubSignIn:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        web: httpx2.Client,
        api: httpx2.Client,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._web = web
        self._api = api

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "allow_signup": "false",
            }
        )
        return f"{str(self._web.base_url).rstrip('/')}/login/oauth/authorize?{query}"

    def login(self, code: str) -> str:
        """Exchange the one-time code for the user's login; the user's token is not kept."""
        exchanged = self._web.post(
            "/login/oauth/access_token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        token = exchanged.json().get("access_token") if exchanged.status_code == 200 else None
        if not isinstance(token, str) or not token:
            raise SignInError("GitHub did not accept the sign-in code")
        user = self._api.get(
            "/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        login = user.json().get("login") if user.status_code == 200 else None
        if not isinstance(login, str) or not _LOGIN.match(login):
            raise SignInError("GitHub did not return a valid login")
        return login


class Permissions:
    """A login's permission on each registered repository, read from GitHub and cached."""

    def __init__(
        self,
        app: GitHubApp,
        installations: Mapping[str, int],
        *,
        ttl: timedelta = timedelta(seconds=60),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._app = app
        self.installations = dict(installations)
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    def level(self, login: str, repository: str) -> str:
        """``admin``, ``write``, ``read`` or ``none``. Unregistered repositories are ``none``."""
        installation = self.installations.get(repository)
        if installation is None:
            return "none"
        now = self._clock()
        with self._lock:
            cached = self._cache.get((login, repository))
            if cached is not None and cached[1] > now:
                return cached[0]
        level = self._app.repository(installation, repository).permission(login)
        with self._lock:
            self._cache[(login, repository)] = (level, now + self._ttl)
        return level

    def can_read(self, login: str, repository: str) -> bool:
        return self.level(login, repository) in READ_LEVELS

    def can_decide(self, login: str, repository: str) -> bool:
        return self.level(login, repository) in DECIDE_LEVELS

    def readable(self, login: str) -> list[str]:
        return [r for r in self.installations if self.can_read(login, r)]

    def repository(self, repository: str) -> Repository:
        """GitHub calls about a registered repository, as its installation."""
        installation = self.installations.get(repository)
        if installation is None:
            raise LookupError(f"{repository} is not a registered repository")
        return self._app.repository(installation, repository)
