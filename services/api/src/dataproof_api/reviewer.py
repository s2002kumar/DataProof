"""The reviewer API: GitHub sign-in and read access to runs (ARCHITECTURE, API surface).

* ``GET /auth/login`` and ``GET /auth/callback``: GitHub's OAuth web flow, guarded by a signed,
  ten-minute state cookie. A successful sign-in sets a signed session cookie (HttpOnly, Secure,
  SameSite=Lax) and keeps nothing else.
* ``GET /api/session``: who is signed in, and the CSRF token that every write must send.
* ``GET /api/runs``, ``GET /api/runs/{id}``, ``…/checks``, ``…/evidence``: runs of the
  registered repositories the reviewer can read on GitHub. A run the reviewer cannot read is
  answered exactly like a run that does not exist (404).
* Writes (``POST``) need the session, the CSRF token in ``X-CSRF-Token`` and an ``Origin`` equal
  to the dashboard's own.
"""

import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic_core import to_jsonable_python

from dataproof_api.reviewers import GitHubSignIn, Permissions, SignInError
from dataproof_api.sessions import CSRF_HEADER, SESSION_COOKIE, STATE_COOKIE, Session, Sessions
from dataproof_contracts import ReviewRun
from dataproof_control import GitHubRecords, RunNotFoundError, RunResults, RunStore


class Reviewer:
    def __init__(
        self,
        store: RunStore,
        results: RunResults,
        records: GitHubRecords,
        sessions: Sessions,
        sign_in: GitHubSignIn,
        permissions: Permissions,
        *,
        public_origin: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.results = results
        self.records = records
        self.sessions = sessions
        self.sign_in = sign_in
        self.permissions = permissions
        self.public_origin = public_origin.rstrip("/")
        """The dashboard's origin (``https://host``); writes from any other origin are refused."""
        self.secure_cookies = self.public_origin.startswith("https://")
        self.clock = clock

    # Dependencies ------------------------------------------------------------------------------

    def session(self, dataproof_session: Annotated[str | None, Cookie()] = None) -> Session:
        session = self.sessions.read(dataproof_session, self.clock())
        if session is None:
            raise HTTPException(401, "sign in first")
        return session

    def writer(
        self,
        request: Request,
        dataproof_session: Annotated[str | None, Cookie()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> Session:
        session = self.session(dataproof_session)
        if request.headers.get("origin") != self.public_origin:
            raise HTTPException(403, "requests must come from the dashboard")
        if x_csrf_token is None or not hmac.compare_digest(
            x_csrf_token.encode(), session.csrf.encode()
        ):
            raise HTTPException(403, f"missing or wrong {CSRF_HEADER}")
        return session

    def readable_run(self, run_id: UUID, login: str) -> ReviewRun:
        try:
            run = self.store.get_run(run_id)
        except RunNotFoundError:
            raise HTTPException(404, "no such run") from None
        if not self.permissions.can_read(login, run.inputs.repository):
            raise HTTPException(404, "no such run")
        return run

    # Routes ------------------------------------------------------------------------------------

    def router(self) -> APIRouter:
        router = APIRouter()
        reader = Annotated[Session, Depends(self.session)]
        writer = Annotated[Session, Depends(self.writer)]

        @router.get("/auth/login")
        def login() -> RedirectResponse:
            state, cookie = self.sessions.new_state(self.clock())
            response = RedirectResponse(self.sign_in.authorize_url(state), status_code=302)
            response.set_cookie(
                STATE_COOKIE,
                cookie,
                max_age=600,
                path="/auth",
                httponly=True,
                secure=self.secure_cookies,
                samesite="lax",
            )
            return response

        @router.get("/auth/callback")
        def callback(
            code: Annotated[str, Query(min_length=1, max_length=200)],
            state: Annotated[str, Query(min_length=1, max_length=200)],
            dataproof_signin: Annotated[str | None, Cookie()] = None,
        ) -> RedirectResponse:
            if not self.sessions.check_state(dataproof_signin, state, self.clock()):
                raise HTTPException(400, "the sign-in expired or did not start here")
            try:
                github_login = self.sign_in.login(code)
            except SignInError as error:
                raise HTTPException(401, str(error)) from None
            value, _ = self.sessions.issue(github_login, self.clock())
            response = RedirectResponse("/", status_code=302)
            response.delete_cookie(STATE_COOKIE, path="/auth")
            response.set_cookie(
                SESSION_COOKIE,
                value,
                max_age=int(self.sessions.lifetime.total_seconds()),
                path="/",
                httponly=True,
                secure=self.secure_cookies,
                samesite="lax",
            )
            return response

        @router.post("/auth/logout", status_code=204)
        def logout(_: writer) -> Response:
            response = Response(status_code=204)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response

        @router.get("/api/session")
        def whoami(session: reader) -> dict[str, Any]:
            return {
                "login": session.login,
                "csrf_token": session.csrf,
                "expires_at": session.expires_at.isoformat(),
                "repositories": self.permissions.readable(session.login),
            }

        @router.get("/api/runs")
        def runs(
            session: reader,
            repository: str | None = None,
            pull_request: Annotated[int | None, Query(gt=0)] = None,
            limit: Annotated[int, Query(gt=0, le=200)] = 50,
        ) -> list[dict[str, Any]]:
            readable = self.permissions.readable(session.login)
            if repository is not None:
                readable = [r for r in readable if r == repository]
            listed = self.store.runs(readable, pull_request=pull_request, limit=limit)
            return [self._summary(run) for run in listed]

        @router.get("/api/runs/{run_id}")
        def run(run_id: UUID, session: reader) -> dict[str, Any]:
            return self._detail(self.readable_run(run_id, session.login))

        @router.get("/api/runs/{run_id}/checks")
        def checks(run_id: UUID, session: reader) -> list[Any]:
            self.readable_run(run_id, session.login)
            return list(to_jsonable_python(self.results.checks(run_id)))

        @router.get("/api/runs/{run_id}/evidence")
        def evidence(run_id: UUID, session: reader) -> dict[str, Any]:
            self.readable_run(run_id, session.login)
            return dict(to_jsonable_python(self.results.evidence(run_id)))

        return router

    # Representations ---------------------------------------------------------------------------

    def _summary(self, run: ReviewRun) -> dict[str, Any]:
        report = self.results.report(run.run_id)
        return {
            "run_id": str(run.run_id),
            "repository": run.inputs.repository,
            "pull_request": run.inputs.pull_request,
            "head_sha": run.inputs.head_sha,
            "trigger": run.trigger.value,
            "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
            "state": run.state.value,
            "verdict": report.verdict.value if report else None,
            "conclusion": report.conclusion.value if report else None,
            "created_at": run.created_at.isoformat(),
        }

    def _detail(self, run: ReviewRun) -> dict[str, Any]:
        run_id = run.run_id
        report = self.results.report(run_id)
        check_run = self.records.check_run(run_id)
        head = (
            self.records.head(run.inputs.repository, run.inputs.pull_request, run.inputs.head_sha)
            if run.inputs.pull_request is not None
            else None
        )
        previews = {
            role.value: {
                key: value
                for key, value in to_jsonable_python(record).items()
                if key != "token_sha256"
            }
            for role, record in self.results.previews(run_id).items()
        }
        return {
            "run": run.model_dump(mode="json"),
            "pull_request_author": head.author if head else None,
            "transitions": to_jsonable_python(self.store.transitions(run_id)),
            "report": to_jsonable_python(report) if report else None,
            "decisions": [d.model_dump(mode="json") for d in self.store.decisions(run_id)],
            "investigations": [
                i.model_dump(mode="json") for i in self.results.investigations(run_id)
            ],
            "reproductions": [
                r.model_dump(mode="json") for r in self.results.reproductions(run_id)
            ],
            "previews": previews,
            "check_run": (
                {
                    "id": check_run.check_run_id,
                    "url": f"https://github.com/{run.inputs.repository}/runs/{check_run.check_run_id}",
                }
                if check_run
                else None
            ),
        }
