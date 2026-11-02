"""GitHub App authentication and the REST calls DataProof makes as the App.

The App proves itself with a short-lived RS256 JWT signed by its private key, and exchanges it
for an installation access token scoped to one repository and to the permissions DataProof
needs. Tokens are cached until shortly before they expire. The key and the tokens stay in the
trusted control plane: they are never passed to a run's containers, logged or put in an error.
"""

import base64
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

API_VERSION = "2022-11-28"
#: What an installation token may do: report checks, read code and pull requests.
PERMISSIONS = {
    "checks": "write",
    "contents": "read",
    "metadata": "read",
    "pull_requests": "read",
}
#: GitHub accepts app JWTs valid for at most ten minutes; ``iat`` is backdated for clock drift.
JWT_LIFETIME = timedelta(minutes=9)
JWT_BACKDATE = timedelta(seconds=60)
#: A cached installation token is replaced this long before it expires.
TOKEN_MARGIN = timedelta(minutes=5)

Clock = Callable[[], datetime]


class GitHubError(RuntimeError):
    """GitHub refused a request. The message carries the status and route, never a token."""

    def __init__(self, status: int, method: str, route: str) -> None:
        super().__init__(f"GitHub answered {status} to {method} {route}")
        self.status = status


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key: RSAPrivateKey, now: datetime) -> str:
    """The App's own credential: an RS256 JWT issued by ``app_id``."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iat": int((now - JWT_BACKDATE).timestamp()),
        "exp": int((now + JWT_LIFETIME).timestamp()),
        "iss": app_id,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def load_private_key(pem: bytes) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("a GitHub App private key is an RSA key")
    return key


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: datetime


class GitHubApp:
    def __init__(
        self,
        app_id: str,
        private_key: RSAPrivateKey,
        *,
        client: httpx2.Client,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._app_id = app_id
        self._key = private_key
        self._client = client
        self._clock = clock
        self._tokens: dict[tuple[int, str], _Token] = {}
        self._lock = threading.Lock()

    def repository(self, installation_id: int, repository: str) -> "Repository":
        """Calls about one repository, as the installation configured for it."""
        return Repository(self, installation_id, repository)

    def installation_token(self, installation_id: int, repository: str) -> str:
        key = (installation_id, repository)
        with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached.expires_at - TOKEN_MARGIN > self._clock():
                return cached.value
        name = repository.split("/", 1)[1]
        body = self._send(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            authorization=f"Bearer {app_jwt(self._app_id, self._key, self._clock())}",
            payload={"repositories": [name], "permissions": PERMISSIONS},
        )
        token = _Token(
            value=body["token"],
            expires_at=datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")),
        )
        with self._lock:
            self._tokens[key] = token
        return token.value

    def _send(
        self,
        method: str,
        route: str,
        *,
        authorization: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self._client.request(
            method,
            route,
            json=payload,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": authorization,
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if response.status_code >= 300:
            raise GitHubError(response.status_code, method, route)
        return response.json() if response.content else None


class Repository:
    """The REST calls DataProof makes about one repository, with its installation token."""

    def __init__(self, app: GitHubApp, installation_id: int, repository: str) -> None:
        self._app = app
        self._installation_id = installation_id
        self.repository = repository

    def _send(self, method: str, route: str, payload: Mapping[str, Any] | None = None) -> Any:
        token = self._app.installation_token(self._installation_id, self.repository)
        return self._app._send(
            method,
            f"/repos/{self.repository}{route}",
            authorization=f"token {token}",
            payload=payload,
        )

    def find_check_run(self, head_sha: str, name: str, external_id: str) -> int | None:
        """The check run with this name and external ID on a commit, if one exists."""
        body = self._send("GET", f"/commits/{head_sha}/check-runs?check_name={name}&per_page=100")
        for check in body.get("check_runs", []):
            if check.get("external_id") == external_id:
                return int(check["id"])
        return None

    def create_check_run(self, payload: Mapping[str, Any]) -> int:
        body = self._send("POST", "/check-runs", payload)
        return int(body["id"])

    def update_check_run(self, check_run_id: int, payload: Mapping[str, Any]) -> None:
        self._send("PATCH", f"/check-runs/{check_run_id}", payload)

    def permission(self, login: str) -> str:
        """The login's permission on this repository: ``admin``, ``write``, ``read`` or ``none``."""
        try:
            body = self._send("GET", f"/collaborators/{login}/permission")
        except GitHubError as error:
            if error.status == 404:
                return "none"
            raise
        level = body.get("permission") if isinstance(body, dict) else None
        return level if level in ("admin", "write", "read") else "none"

    def maintainers(self) -> list[str]:
        """Collaborators who can push: the maintainers the approver rule counts (ADR-0005)."""
        body = self._send("GET", "/collaborators?permission=push&per_page=100")
        return sorted(str(collaborator["login"]) for collaborator in body)

    def has_commit(self, sha: str) -> bool:
        try:
            self._send("GET", f"/commits/{sha}")
        except GitHubError as error:
            if error.status in (404, 422):
                return False
            raise
        return True
