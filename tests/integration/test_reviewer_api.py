"""The reviewer API: GitHub sign-in, sessions, CSRF and per-repository read access."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from control_env import (
    DATASET_DIGEST,
    FAST_RETRY,
    HEAD_SHA,
    NEW_HEAD_SHA,
    RUN_TTL,
    create,
    run_inputs,
)
from dataproof_api import Intake, Registration, create_app
from dataproof_api.gateway import Grants
from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_api.reviewer import PreviewLinks, Reviewer
from dataproof_api.reviewers import GitHubSignIn, Permissions
from dataproof_api.sessions import CSRF_HEADER, SESSION_COOKIE, Sessions
from dataproof_contracts import (
    CandidateBinding,
    CheckOutcome,
    CheckResult,
    EvidenceManifest,
    NotRunReason,
    RunState,
    RunTrigger,
)
from dataproof_control import (
    CheckRecord,
    GitHubRecords,
    ManifestRecord,
    ReproductionQueue,
    RunResults,
    RunStore,
)
from dataproof_control.results import record_check, record_manifest
from dataproof_runner.project import ProjectConfig
from github_env import (
    APP_ID,
    CLIENT_ID,
    CLIENT_SECRET,
    INSTALLATION,
    PRIVATE_KEY_PEM,
    REPOSITORY,
    FakeGitHub,
    pull_request_event,
)

pytestmark = pytest.mark.integration

ORIGIN = "https://dataproof.test"
SECRET = b"session-secret-for-tests-0123456789abcdef"
OTHER_REPOSITORY = "example-org/elsewhere"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class Stack:
    def __init__(self, store: RunStore, dsn: str) -> None:
        self.store = store
        self.dsn = dsn
        self.clock = Clock()
        self.github = FakeGitHub(
            {INSTALLATION: {REPOSITORY}},
            permissions={
                REPOSITORY: {"maintainer": "write", "viewer": "read", "developer": "write"}
            },
        )
        app = GitHubApp(APP_ID, load_private_key(PRIVATE_KEY_PEM), client=self.github.client())
        self.records = GitHubRecords(dsn)
        self.grants = Grants(b"preview-grant-secret-for-tests-0123456789")
        project = ProjectConfig(
            repository=REPOSITORY, runtime_image="sha256:" + "a" * 64, baseline="commerce-v1"
        )
        self.intake = Intake(
            store,
            self.records,
            {REPOSITORY: Registration(project, INSTALLATION, DATASET_DIGEST, "0.1.0")},
            run_ttl=RUN_TTL,
            retry=FAST_RETRY,
        )
        reviewer = Reviewer(
            store,
            RunResults(dsn),
            self.records,
            Sessions(SECRET),
            GitHubSignIn(
                CLIENT_ID,
                CLIENT_SECRET,
                f"{ORIGIN}/auth/callback",
                web=self.github.client(),
                api=self.github.client(),
            ),
            Permissions(app, {REPOSITORY: INSTALLATION}, clock=self.clock),
            configs={REPOSITORY: project},
            public_origin=ORIGIN,
            run_ttl=RUN_TTL,
            retry=FAST_RETRY,
            previews=PreviewLinks(self.grants, "previews.test"),
            reproductions=ReproductionQueue(dsn),
            clock=self.clock,
        )
        self.app = create_app(self.intake, webhook_secret=b"webhook-secret", reviewer=reviewer)

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=ORIGIN)

    def signed_in(self, login: str) -> tuple[TestClient, str]:
        """A browser signed in as ``login``, and its CSRF token."""
        client = self.client()
        code = f"code-{uuid4()}"
        self.github.oauth_codes[code] = login
        started = client.get("/auth/login", follow_redirects=False)
        assert started.status_code == 302
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        finished = client.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        assert finished.status_code == 302, finished.text
        session = client.get("/api/session")
        assert session.status_code == 200
        return client, session.json()["csrf_token"]

    def webhook_run(self) -> UUID:
        received = self.intake.receive(f"d-{uuid4()}", "pull_request", pull_request_event())
        assert received.run_id is not None
        return received.run_id


@pytest.fixture
def stack(store: RunStore, control_dsn: str) -> Stack:
    return Stack(store, control_dsn)


# Sign-in and sessions --------------------------------------------------------------------------


def test_sign_in_sets_a_signed_session_and_keeps_no_github_token(stack: Stack) -> None:
    client = stack.client()
    stack.github.oauth_codes["code-1"] = "maintainer"
    started = client.get("/auth/login", follow_redirects=False)
    location = urlparse(started.headers["location"])
    query = parse_qs(location.query)
    assert location.path == "/login/oauth/authorize"
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{ORIGIN}/auth/callback"]
    finished = client.get(
        "/auth/callback",
        params={"code": "code-1", "state": query["state"][0]},
        follow_redirects=False,
    )
    cookie = finished.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE}=" in cookie
    for attribute in ("httponly", "secure", "samesite=lax", "path=/"):
        assert attribute in cookie
    session = client.get("/api/session").json()
    assert session["login"] == "maintainer"
    assert session["repositories"] == [REPOSITORY]
    (user_token,) = stack.github.user_tokens
    assert user_token not in finished.headers["set-cookie"]
    assert user_token not in str(session)


@pytest.mark.parametrize("problem", ["state from elsewhere", "no state cookie", "bad code"])
def test_a_sign_in_that_did_not_start_here_or_fails_is_refused(stack: Stack, problem: str) -> None:
    client = stack.client()
    stack.github.oauth_codes["code-1"] = "maintainer"
    state = parse_qs(
        urlparse(client.get("/auth/login", follow_redirects=False).headers["location"]).query
    )["state"][0]
    params = {"code": "code-1", "state": state}
    if problem == "state from elsewhere":
        params["state"] = "attacker-chosen-state"
    elif problem == "no state cookie":
        client = stack.client()
    else:
        params["code"] = "unknown-code"
    response = client.get("/auth/callback", params=params, follow_redirects=False)
    assert response.status_code == (401 if problem == "bad code" else 400)
    assert client.get("/api/session").status_code == 401


def test_a_missing_tampered_or_expired_session_is_refused(stack: Stack) -> None:
    assert stack.client().get("/api/runs").status_code == 401
    client, _ = stack.signed_in("viewer")
    value = client.cookies[SESSION_COOKIE]
    forged = stack.client()
    payload, signature = value.split(".")
    forged.cookies.set(SESSION_COOKIE, f"{payload}.{signature[:-2]}AA")
    assert forged.get("/api/runs").status_code == 401
    stack.clock.now += timedelta(hours=8, seconds=1)
    assert client.get("/api/runs").status_code == 401


# Read access -----------------------------------------------------------------------------------


def test_runs_are_listed_only_from_repositories_the_reviewer_can_read(stack: Stack) -> None:
    run_id = stack.webhook_run()
    create(stack.store, run_inputs(repository=OTHER_REPOSITORY))
    viewer, _ = stack.signed_in("viewer")
    listed = viewer.get("/api/runs").json()
    assert [r["run_id"] for r in listed] == [str(run_id)]
    assert listed[0]["head_sha"] == HEAD_SHA

    stranger, _ = stack.signed_in("stranger")
    assert stranger.get("/api/runs").json() == []
    hidden = stranger.get(f"/api/runs/{run_id}")
    missing = stranger.get(f"/api/runs/{uuid4()}")
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json()), (
        "an unreadable run looks exactly like a missing one"
    )
    assert stranger.get(f"/api/runs/{run_id}/checks").status_code == 404


def test_a_run_shows_its_history_author_and_check_run(stack: Stack) -> None:
    run_id = stack.webhook_run()
    viewer, _ = stack.signed_in("viewer")
    detail = viewer.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["run_id"] == str(run_id)
    assert detail["pull_request_author"] == "developer"
    assert detail["transitions"][0]["actor"] == "github:developer"
    assert detail["report"] is None
    assert detail["decisions"] == []
    assert detail["check_run"] is None


def awaiting_binding(stack: Stack, run_id: UUID, head: str = HEAD_SHA) -> CandidateBinding:
    """Record a check of the run that waits on a binding, as VERIFYING would."""
    binding = CandidateBinding(
        source_path="dataproof/bindings/c1.toml",
        head_sha=head,
        query_sql="SELECT source_event_id, amount FROM ledger_entries",
        column_mapping={"event_id": "source_event_id", "amount": "amount"},
    )
    result = CheckResult(
        check_id="commerce.c1_event_preservation",
        contract_id="commerce.c1_event_preservation",
        contract_version=1,
        required=True,
        outcome=CheckOutcome.NOT_RUN,
        not_run_reason=NotRunReason.AWAITING_BINDING_REVIEW,
        dataset_digest=DATASET_DIGEST,
    )
    with psycopg.connect(stack.dsn) as conn:
        record_check(conn, run_id, CheckRecord(result, binding=binding))
    return binding


def test_checks_show_the_binding_a_reviewer_would_decide_on(stack: Stack) -> None:
    run_id = stack.webhook_run()
    binding = awaiting_binding(stack, run_id)
    viewer, _ = stack.signed_in("viewer")
    (check,) = viewer.get(f"/api/runs/{run_id}/checks").json()
    assert check["result"]["not_run_reason"] == "AWAITING_BINDING_REVIEW"
    assert check["binding"]["query_sql"] == binding.query_sql
    assert check["binding_digest"] == binding.digest()
    assert viewer.get(f"/api/runs/{run_id}/evidence").json() == {}


def test_losing_access_takes_effect_within_the_permission_cache(stack: Stack) -> None:
    run_id = stack.webhook_run()
    viewer, _ = stack.signed_in("viewer")
    assert viewer.get(f"/api/runs/{run_id}").status_code == 200
    del stack.github.permissions[REPOSITORY]["viewer"]
    assert viewer.get(f"/api/runs/{run_id}").status_code == 200, "cached for up to a minute"
    stack.clock.now += timedelta(seconds=61)
    assert viewer.get(f"/api/runs/{run_id}").status_code == 404


# Writes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 403),
        ({"Origin": ORIGIN}, 403),
        ({"Origin": ORIGIN, CSRF_HEADER: "guessed"}, 403),
        ({"Origin": "https://attacker.test", CSRF_HEADER: "<csrf>"}, 403),
        ({CSRF_HEADER: "<csrf>"}, 403),
        ({"Origin": ORIGIN, CSRF_HEADER: "<csrf>"}, 204),
    ],
)
def test_writes_need_the_csrf_token_and_the_dashboard_origin(
    stack: Stack, headers: dict[str, Any], status: int
) -> None:
    client, csrf = stack.signed_in("viewer")
    sent = {k: (csrf if v == "<csrf>" else v) for k, v in headers.items()}
    response = client.post("/auth/logout", headers=sent)
    assert response.status_code == status
    if status == 204:
        assert f"{SESSION_COOKIE}=" in response.headers["set-cookie"]
        assert "max-age=0" in response.headers["set-cookie"].lower()


def post(client: TestClient, csrf: str, path: str, body: dict[str, Any]) -> Any:
    return client.post(path, json=body, headers={"Origin": ORIGIN, CSRF_HEADER: csrf})


def test_a_maintainer_approves_a_binding_and_a_new_run_starts(stack: Stack) -> None:
    run_id = stack.webhook_run()
    digest = awaiting_binding(stack, run_id).digest()
    client, csrf = stack.signed_in("maintainer")
    response = post(
        client,
        csrf,
        f"/api/runs/{run_id}/binding-decisions",
        {"action": "APPROVE_BINDING", "binding_digest": digest, "comment": "Reads the ledger."},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["recorded"] is True
    decision = body["decision"]
    assert (decision["actor"], decision["pr_author"]) == ("maintainer", "developer")
    assert decision["approver_separation"] == "ORGANIZATIONAL"
    assert decision["head_sha"] == HEAD_SHA
    started = stack.store.get_run(UUID(body["started_run_id"]))
    assert (started.trigger, started.parent_run_id) == (RunTrigger.BINDING_APPROVAL, run_id)
    assert stack.store.transitions(started.run_id)[0].actor == "user:maintainer"

    again = post(
        client,
        csrf,
        f"/api/runs/{run_id}/binding-decisions",
        {"action": "APPROVE_BINDING", "binding_digest": digest},
    )
    assert (again.status_code, again.json()["recorded"]) == (201, False)
    assert again.json()["started_run_id"] == body["started_run_id"]
    contradicting = post(
        client,
        csrf,
        f"/api/runs/{run_id}/binding-decisions",
        {"action": "REJECT_BINDING", "binding_digest": digest},
    )
    assert contradicting.status_code == 409


@pytest.mark.parametrize(
    ("maintainers", "status", "separation"),
    [
        ({"developer": "write", "maintainer": "write"}, 403, None),
        ({"developer": "admin", "viewer": "read"}, 201, "PROCEDURAL"),
    ],
)
def test_the_author_approves_only_when_there_is_no_other_maintainer(
    stack: Stack, maintainers: dict[str, str], status: int, separation: str | None
) -> None:
    stack.github.permissions[REPOSITORY] = maintainers
    run_id = stack.webhook_run()
    digest = awaiting_binding(stack, run_id).digest()
    client, csrf = stack.signed_in("developer")
    response = post(
        client,
        csrf,
        f"/api/runs/{run_id}/binding-decisions",
        {"action": "APPROVE_BINDING", "binding_digest": digest},
    )
    assert response.status_code == status, response.text
    if separation is not None:
        assert response.json()["decision"]["approver_separation"] == separation
    else:
        assert "other maintainers" in response.json()["detail"]
        assert stack.store.decisions(run_id) == []


@pytest.mark.parametrize(("login", "status"), [("viewer", 403), ("stranger", 404)])
def test_only_writers_decide(stack: Stack, login: str, status: int) -> None:
    run_id = stack.webhook_run()
    digest = awaiting_binding(stack, run_id).digest()
    client, csrf = stack.signed_in(login)
    response = post(
        client,
        csrf,
        f"/api/runs/{run_id}/binding-decisions",
        {"action": "REJECT_BINDING", "binding_digest": digest},
    )
    assert response.status_code == status
    assert stack.store.decisions(run_id) == []


def test_a_decision_must_name_a_binding_the_run_waits_on_at_its_current_head(
    stack: Stack,
) -> None:
    run_id = stack.webhook_run()
    digest = awaiting_binding(stack, run_id).digest()
    client, csrf = stack.signed_in("maintainer")
    unknown = post(
        client,
        csrf,
        f"/api/runs/{run_id}/binding-decisions",
        {"action": "APPROVE_BINDING", "binding_digest": "0" * 64},
    )
    assert unknown.status_code == 422
    stack.intake.receive(
        "d-new", "pull_request", pull_request_event("synchronize", head=NEW_HEAD_SHA)
    )
    stale = post(
        client,
        csrf,
        f"/api/runs/{run_id}/binding-decisions",
        {"action": "APPROVE_BINDING", "binding_digest": digest},
    )
    assert stale.status_code == 409
    assert NEW_HEAD_SHA in stale.json()["detail"]


def test_a_reviewer_cancels_a_run_and_the_cancellation_is_recorded(stack: Stack) -> None:
    run_id = stack.webhook_run()
    client, csrf = stack.signed_in("maintainer")
    response = post(client, csrf, f"/api/runs/{run_id}/cancel", {"comment": "Wrong base."})
    assert response.status_code == 200
    assert stack.store.get_run(run_id).state is RunState.CANCELLED
    (decision,) = stack.store.decisions(run_id)
    assert (decision.action.value, decision.actor) == ("CANCEL_RUN", "maintainer")
    assert post(client, csrf, f"/api/runs/{run_id}/cancel", {}).status_code == 409


def test_a_correction_is_requested_as_a_new_run_of_an_existing_commit(stack: Stack) -> None:
    run_id = stack.webhook_run()
    correction = "1f2e3d4c5b6a79881f2e3d4c5b6a79881f2e3d4c"
    client, csrf = stack.signed_in("developer")
    unknown = post(client, csrf, f"/api/runs/{run_id}/correction-replay", {"head_sha": correction})
    assert unknown.status_code == 422
    same = post(client, csrf, f"/api/runs/{run_id}/correction-replay", {"head_sha": HEAD_SHA})
    assert same.status_code == 422

    stack.github.commits.add(correction)
    response = post(client, csrf, f"/api/runs/{run_id}/correction-replay", {"head_sha": correction})
    assert response.status_code == 201, response.text
    started = stack.store.get_run(UUID(response.json()["run_id"]))
    assert (started.trigger, started.parent_run_id) == (RunTrigger.CORRECTION_REPLAY, run_id)
    assert started.inputs.head_sha == correction
    (decision,) = stack.store.decisions(run_id)
    assert (decision.action.value, decision.head_sha) == ("REQUEST_CORRECTION_REPLAY", correction)
    assert stack.store.get_run(run_id).state is RunState.QUEUED, "the original is untouched"


@pytest.mark.parametrize(("login", "status"), [("viewer", 200), ("stranger", 404)])
def test_a_preview_link_is_a_grant_for_one_run_role_and_reviewer(
    stack: Stack, login: str, status: int
) -> None:
    run_id = stack.webhook_run()
    client, csrf = stack.signed_in(login)
    response = post(client, csrf, f"/api/runs/{run_id}/previews/candidate", {})
    assert response.status_code == status
    if status != 200:
        return
    url = urlparse(response.json()["url"])
    assert (url.scheme, url.hostname, url.path) == (
        "https",
        f"candidate-{run_id}.previews.test",
        "/__dataproof/grant",
    )
    grant = stack.grants.read(parse_qs(url.query)["token"][0], stack.clock())
    assert grant is not None
    assert (grant.run_id, grant.role.value, grant.login) == (run_id, "CANDIDATE", "viewer")
    assert client.post(f"/api/runs/{run_id}/previews/candidate").status_code == 403, "needs CSRF"


EXAMPLES = Path(__file__).parents[2] / "packages" / "contracts" / "examples"


def failed_and_sealed(stack: Stack, run_id: UUID, *, sealed: bool = True) -> str:
    """Record F1's failed C1 check and, if ``sealed``, the run's evidence manifest."""
    failed = CheckResult.model_validate_json(
        (EXAMPLES / "CheckResult.f1-detected.json").read_text()
    )
    manifest = EvidenceManifest.model_validate(
        {
            **json.loads((EXAMPLES / "EvidenceManifest.sealed.json").read_text()),
            "run_id": str(run_id),
        }
    )
    with psycopg.connect(stack.dsn) as conn:
        record_check(conn, run_id, CheckRecord(failed))
        if sealed:
            record_manifest(conn, run_id, ManifestRecord("runs/manifest.json", "a" * 64, manifest))
    return failed.check_id


def test_a_reproduction_is_queued_for_a_failed_check_of_a_sealed_run(stack: Stack) -> None:
    run_id = stack.webhook_run()
    check_id = failed_and_sealed(stack, run_id)
    awaiting_binding(stack, run_id)
    client, csrf = stack.signed_in("maintainer")
    path = f"/api/runs/{run_id}/reproductions"

    not_failed = post(client, csrf, path, {"check_id": "commerce.c1_event_preservation"})
    assert not_failed.status_code == 422
    queued = post(client, csrf, path, {"check_id": check_id})
    assert queued.status_code == 202, queued.text
    assert queued.json()["created"] is True
    again = post(client, csrf, path, {"check_id": check_id})
    assert again.json()["created"] is False
    assert again.json()["request"]["request_id"] == queued.json()["request"]["request_id"]
    (request,) = client.get(f"/api/runs/{run_id}").json()["reproduction_requests"]
    assert (request["check_id"], request["state"], request["requested_by"]) == (
        check_id,
        "queued",
        "maintainer",
    )
    viewer, viewer_csrf = stack.signed_in("viewer")
    assert post(viewer, viewer_csrf, path, {"check_id": check_id}).status_code == 403


def test_a_run_that_has_not_sealed_its_evidence_cannot_be_reproduced_yet(stack: Stack) -> None:
    run_id = stack.webhook_run()
    check_id = failed_and_sealed(stack, run_id, sealed=False)
    client, csrf = stack.signed_in("maintainer")
    response = post(client, csrf, f"/api/runs/{run_id}/reproductions", {"check_id": check_id})
    assert response.status_code == 409
