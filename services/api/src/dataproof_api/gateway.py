"""The signed preview gateway (ARCHITECTURE, shadow preview; THREAT_MODEL; #4).

Remote reviewers reach a run's live previews only through this gateway.

* **A separate origin per preview.** A preview runs the pull request's own frontend code, so it
  is served from its own host, ``<role>-<run_id>.<preview domain>``, never from the dashboard's
  origin (where that code could read the reviewer's CSRF token and act for them). The preview
  domain should be a separate registrable domain from the dashboard's.
* **The host selects; it never targets.** The host names a run and a role. The upstream is
  always the edge that the trusted supervisor registered for that run and role (the backend's
  ``preview_access``); no host, port or URL from the client is ever connected to.
* **Signed, expiring, bound grants.** The reviewer API issues a grant for one run, one role and
  one reviewer, valid for an hour. ``/__dataproof/grant?token=…`` on the preview host turns it
  into a host-only, HTTP-only cookie. Every request re-verifies the grant, re-checks the
  reviewer's permission on the repository and resolves the registered upstream again.
* **A narrow proxy.** ``GET`` and ``HEAD`` only; an allowlist of request headers, plus the
  edge's own run token; no cookie the preview sets reaches the browser; redirects to the edge
  are made relative.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx2
from fastapi import Cookie, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from dataproof_api.reviewers import Permissions
from dataproof_api.sessions import Signer
from dataproof_contracts import Environment
from dataproof_control import RunNotFoundError, RunStore
from dataproof_runner.backend import ExecutionBackend, PreviewAccess

GRANT_COOKIE = "dataproof_grant"
EDGE_COOKIE = "dataproof_preview"
GRANT_PATH = "__dataproof/grant"
GRANT_LIFETIME = timedelta(hours=1)
_HOST = re.compile(r"^(reference|candidate)-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.(.+)$")
#: Request headers a preview may see. Everything else, including cookies, authorization and
#: forwarding headers, is dropped.
FORWARDED_HEADERS = frozenset(
    {"accept", "accept-language", "if-modified-since", "if-none-match", "range", "user-agent"}
)
#: Response headers never passed back: cookies, hop-by-hop headers, and encodings the proxy
#: has already undone.
DROPPED_HEADERS = frozenset(
    {
        "set-cookie",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-encoding",
        "content-length",
        "proxy-authenticate",
        "upgrade",
    }
)

#: The registered preview of a run's role, or None if it is not serving.
Resolver = Callable[[UUID, Environment], PreviewAccess | None]


def backend_resolver(backend: ExecutionBackend) -> Resolver:
    """Previews as the execution backend registered them for each run and role."""

    def resolve(run_id: UUID, role: Environment) -> PreviewAccess | None:
        handle = backend.attach(run_id, role)
        return backend.preview_access(handle) if handle is not None else None

    return resolve


@dataclass(frozen=True)
class Grant:
    run_id: UUID
    role: Environment
    login: str
    expires_at: datetime


class Grants:
    def __init__(self, secret: bytes, *, lifetime: timedelta = GRANT_LIFETIME) -> None:
        self._signer = Signer(secret, "preview grant")
        self.lifetime = lifetime

    def issue(self, run_id: UUID, role: Environment, login: str, now: datetime) -> str:
        payload = {"run": str(run_id), "role": role.value, "login": login}
        return self._signer.sign(payload, now + self.lifetime)

    def read(self, value: str | None, now: datetime) -> Grant | None:
        payload = self._signer.verify(value, now)
        if payload is None:
            return None
        try:
            return Grant(
                UUID(str(payload["run"])),
                Environment(payload["role"]),
                str(payload["login"]),
                datetime.fromtimestamp(int(payload["exp"]), UTC),
            )
        except (KeyError, ValueError):
            return None


def preview_host(role: Environment, run_id: UUID, domain: str) -> str:
    return f"{role.value.lower()}-{run_id}.{domain}"


class Gateway:
    def __init__(
        self,
        store: RunStore,
        grants: Grants,
        permissions: Permissions,
        resolve: Resolver,
        *,
        domain: str,
        upstream: httpx2.Client,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.grants = grants
        self.permissions = permissions
        self.resolve = resolve
        self.domain = domain.lower()
        self.upstream = upstream
        """Only ever sent to registered edges; it must not follow redirects."""
        self.secure_cookies = True
        self.clock = clock

    def _target(self, request: Request) -> tuple[UUID, Environment]:
        host = (request.headers.get("host") or "").lower().rsplit(":", 1)[0]
        match = _HOST.match(host)
        if match is None or match.group(3) != self.domain:
            raise HTTPException(404, "not a preview")
        return UUID(match.group(2)), Environment(match.group(1).upper())

    def app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get(f"/{GRANT_PATH}")
        def grant(
            request: Request, token: Annotated[str, Query(min_length=1, max_length=2000)]
        ) -> RedirectResponse:
            run_id, role = self._target(request)
            now = self.clock()
            granted = self.grants.read(token, now)
            if granted is None or (granted.run_id, granted.role) != (run_id, role):
                raise HTTPException(401, "open this preview from the DataProof dashboard")
            response = RedirectResponse("/", status_code=302)
            response.set_cookie(
                GRANT_COOKIE,
                token,
                max_age=max(int((granted.expires_at - now).total_seconds()), 0),
                path="/",
                httponly=True,
                secure=self.secure_cookies,
                samesite="lax",
            )
            return response

        @app.api_route("/{path:path}", methods=["GET", "HEAD"])
        def proxy(
            request: Request,
            path: str,
            dataproof_grant: Annotated[str | None, Cookie()] = None,
        ) -> Response:
            return self._proxy(request, path, dataproof_grant)

        @app.api_route("/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        def refuse(path: str) -> Response:
            del path
            raise HTTPException(405, "previews are read-only")

        return app

    def _proxy(self, request: Request, path: str, cookie: str | None) -> Response:
        run_id, role = self._target(request)
        granted = self.grants.read(cookie, self.clock())
        if granted is None:
            raise HTTPException(401, "open this preview from the DataProof dashboard")
        if (granted.run_id, granted.role) != (run_id, role):
            raise HTTPException(403, "this grant is for another preview")
        try:
            run = self.store.get_run(run_id)
        except RunNotFoundError:
            raise HTTPException(404, "not a preview") from None
        if not self.permissions.can_read(granted.login, run.inputs.repository):
            raise HTTPException(403, "you can no longer read this repository")
        access = self.resolve(run_id, role)
        if access is None:
            raise HTTPException(503, "this preview is not running")

        edge = httpx2.URL(access.url)
        target = edge.copy_with(
            path="/" + quote(path.lstrip("/"), safe="/:@!$&'()*+,;=-._~"),
            query=request.url.query.encode() or None,
        )
        if (target.scheme, target.host, target.port) != (edge.scheme, edge.host, edge.port):
            raise HTTPException(400, "bad preview path")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED_HEADERS}
        headers["Cookie"] = f"{EDGE_COOKIE}={access.token}"
        try:
            upstream = self.upstream.request(request.method, target, headers=headers)
        except httpx2.HTTPError:
            raise HTTPException(502, "the preview did not answer") from None
        return Response(
            content=b"" if request.method == "HEAD" else upstream.content,
            status_code=upstream.status_code,
            headers=self._response_headers(upstream, edge),
        )

    @staticmethod
    def _response_headers(upstream: Any, edge: httpx2.URL) -> dict[str, str]:
        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in DROPPED_HEADERS}
        location = headers.get("location")
        if location is not None:
            parts = urlsplit(location)
            if parts.netloc and parts.netloc != f"{edge.host}:{edge.port}":
                # Never redirect a reviewer to somewhere the preview names.
                headers.pop("location")
            elif parts.netloc:
                headers["location"] = parts.path + (f"?{parts.query}" if parts.query else "")
        return headers
