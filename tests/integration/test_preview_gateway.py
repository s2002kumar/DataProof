"""The signed preview gateway, against a fake edge: it can reach nothing but registered previews.

Issue #4's negative tests: a client-supplied upstream, an unregistered preview, and a stale,
expired, tampered or foreign grant are all denied, and nothing reaches an upstream that the
supervisor did not register.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from control_env import create
from dataproof_api.gateway import EDGE_COOKIE, GRANT_COOKIE, GRANT_PATH, Gateway, Grants
from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_api.reviewers import Permissions
from dataproof_contracts import Environment
from dataproof_control import RunStore
from dataproof_runner.backend import PreviewAccess
from github_env import APP_ID, INSTALLATION, PRIVATE_KEY_PEM, REPOSITORY, FakeGitHub

pytestmark = pytest.mark.integration

DOMAIN = "previews.test"
EDGE = "http://127.0.0.1:41234"
EDGE_TOKEN = "edge-run-token"  # noqa: S105 - a placeholder, not a credential
REFERENCE, CANDIDATE = Environment.REFERENCE, Environment.CANDIDATE


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeEdge:
    """Stands in for a registered edge and records every request that reaches it."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.location: str | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        headers = {"Set-Cookie": "tracker=1; Path=/", "X-DataProof-Commit": "a" * 40}
        if self.location is not None:
            return httpx2.Response(302, headers={**headers, "Location": self.location})
        return httpx2.Response(200, headers=headers, json={"path": request.url.path})


class Setup:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.clock = Clock()
        self.run_id = create(store).run.run_id
        self.github = FakeGitHub(
            {INSTALLATION: {REPOSITORY}}, permissions={REPOSITORY: {"viewer": "read"}}
        )
        app = GitHubApp(APP_ID, load_private_key(PRIVATE_KEY_PEM), client=self.github.client())
        self.grants = Grants(b"preview-grant-secret-for-tests-0123456789")
        self.edge = FakeEdge()
        self.registered = {(self.run_id, REFERENCE), (self.run_id, CANDIDATE)}
        self.gateway = Gateway(
            store,
            self.grants,
            Permissions(app, {REPOSITORY: INSTALLATION}, clock=self.clock),
            self.resolve,
            domain=DOMAIN,
            upstream=httpx2.Client(transport=httpx2.MockTransport(self.edge.handle)),
            clock=self.clock,
        )

    def resolve(self, run_id: UUID, role: Environment) -> PreviewAccess | None:
        if (run_id, role) not in self.registered:
            return None
        return PreviewAccess(EDGE, EDGE_TOKEN)

    def browser(self, role: Environment = REFERENCE, run_id: UUID | None = None) -> TestClient:
        host = f"{role.value.lower()}-{run_id or self.run_id}.{DOMAIN}"
        return TestClient(self.gateway.app(), base_url=f"https://{host}")

    def opened(self, role: Environment = REFERENCE, login: str = "viewer") -> TestClient:
        browser = self.browser(role)
        token = self.grants.issue(self.run_id, role, login, self.clock())
        response = browser.get(f"/{GRANT_PATH}", params={"token": token}, follow_redirects=False)
        assert response.status_code == 302
        return browser


@pytest.fixture
def setup(store: RunStore) -> Setup:
    return Setup(store)


def test_a_grant_opens_the_preview_and_only_the_registered_edge_is_reached(setup: Setup) -> None:
    browser = setup.browser()
    token = setup.grants.issue(setup.run_id, REFERENCE, "viewer", setup.clock())
    opened = browser.get(f"/{GRANT_PATH}", params={"token": token}, follow_redirects=False)
    cookie = opened.headers["set-cookie"].lower()
    for attribute in ("httponly", "secure", "samesite=lax", "path=/"):
        assert attribute in cookie
    assert "domain=" not in cookie, "the grant stays on this preview's host"

    response = browser.get(
        "/api/orders/1/refunds?kind=REFUND",
        headers={"Authorization": "Bearer stolen", "X-Forwarded-Host": "evil.example"},
    )
    assert response.status_code == 200
    assert response.json() == {"path": "/api/orders/1/refunds"}
    assert "set-cookie" not in response.headers, "no cookie the preview sets reaches the browser"
    assert response.headers["x-dataproof-commit"] == "a" * 40
    (sent,) = setup.edge.requests
    assert str(sent.url) == f"{EDGE}/api/orders/1/refunds?kind=REFUND"
    assert sent.headers["cookie"] == f"{EDGE_COOKIE}={EDGE_TOKEN}"
    assert "authorization" not in sent.headers
    assert "x-forwarded-host" not in sent.headers
    assert GRANT_COOKIE not in sent.headers["cookie"]


@pytest.mark.parametrize(
    "problem", ["no grant", "expired", "tampered", "another run", "another role"]
)
def test_a_missing_stale_tampered_or_foreign_grant_is_denied(setup: Setup, problem: str) -> None:
    browser = setup.browser(REFERENCE)
    now = setup.clock()
    if problem != "no grant":
        run_id = uuid4() if problem == "another run" else setup.run_id
        role = CANDIDATE if problem == "another role" else REFERENCE
        token = setup.grants.issue(run_id, role, "viewer", now)
        if problem == "tampered":
            token = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
        browser.cookies.set(GRANT_COOKIE, token)
        if problem == "expired":
            setup.clock.now += timedelta(hours=1, seconds=1)
    response = browser.get("/")
    expected = 403 if problem in ("another run", "another role") else 401
    assert response.status_code == expected
    assert setup.edge.requests == [], "nothing reached an upstream"


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/steal",
        "/http://169.254.169.254/latest/meta-data/",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/..%2f..%2fadmin",
        "/@evil.example/",
    ],
)
def test_a_client_supplied_upstream_is_never_contacted(setup: Setup, path: str) -> None:
    browser = setup.opened()
    browser.get(path)
    for sent in setup.edge.requests:
        assert (sent.url.scheme, sent.url.host, sent.url.port) == ("http", "127.0.0.1", 41234)


def test_unregistered_and_foreign_hosts_are_not_previews(setup: Setup) -> None:
    other = uuid4()
    token = setup.grants.issue(other, REFERENCE, "viewer", setup.clock())
    unknown = setup.browser(run_id=other)
    unknown.cookies.set(GRANT_COOKIE, token)
    assert unknown.get("/").status_code == 404
    foreign = TestClient(
        setup.gateway.app(), base_url=f"https://reference-{setup.run_id}.evil.example"
    )
    assert foreign.get("/").status_code == 404
    setup.registered.clear()
    assert setup.opened().get("/").status_code == 503, "the preview is not running"
    assert setup.edge.requests == []


def test_previews_are_read_only(setup: Setup) -> None:
    browser = setup.opened()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert browser.request(method, "/api/orders/1").status_code == 405
    assert setup.edge.requests == []


def test_losing_access_closes_the_preview(setup: Setup) -> None:
    browser = setup.opened()
    assert browser.get("/").status_code == 200
    del setup.github.permissions[REPOSITORY]["viewer"]
    setup.clock.now += timedelta(seconds=61)
    assert browser.get("/").status_code == 403


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (f"{EDGE}/orders/2?page=1", "/orders/2?page=1"),
        ("https://evil.example/phish", None),
        ("/orders/3", "/orders/3"),
    ],
)
def test_redirects_never_leave_the_preview(
    setup: Setup, location: str, expected: str | None
) -> None:
    browser = setup.opened()
    setup.edge.location = location
    response = browser.get("/orders/1", follow_redirects=False)
    assert response.headers.get("location") == expected
