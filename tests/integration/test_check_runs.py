"""Commit-bound check runs, published from the control database to a fake GitHub."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from control_env import (
    HEAD_SHA,
    LEASE,
    NEW_HEAD_SHA,
    REPOSITORY,
    create,
    pass_deadline,
    run_inputs,
)
from dataproof_api.checks import CHECK_NAME, CheckPublisher
from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_contracts import (
    ErrorCode,
    GitHubCheckConclusion,
    NotExecutedReason,
    ReportVerdict,
    RunState,
)
from dataproof_control import (
    Claim,
    GitHubRecords,
    ReportRecord,
    RunResults,
    RunStore,
)
from dataproof_control.results import record_report
from github_env import APP_ID, INSTALLATION, PRIVATE_KEY_PEM, FakeGitHub

pytestmark = pytest.mark.integration
S = RunState

BLOCKED = ReportRecord(
    verdict=ReportVerdict.BLOCKED,
    conclusion=GitHubCheckConclusion.FAILURE,
    report={
        "coverage": [
            {
                "check_id": "migration.execute",
                "required": True,
                "outcome": "PASS",
                "reason": None,
                "mismatch_count": 0,
            },
            {
                "check_id": "commerce.c1_event_preservation",
                "required": True,
                "outcome": "FAIL",
                "reason": None,
                "mismatch_count": 1,
            },
        ],
        "investigations": [{"model": "model-off", "status": "COMPLETED"}],
    },
)


class Setup:
    def __init__(self, store: RunStore, dsn: str) -> None:
        self.store = store
        self.records = GitHubRecords(dsn)
        self.github = FakeGitHub({INSTALLATION: {REPOSITORY}})
        app = GitHubApp(APP_ID, load_private_key(PRIVATE_KEY_PEM), client=self.github.client())
        self.publisher = CheckPublisher(
            store,
            RunResults(dsn),
            self.records,
            app,
            {REPOSITORY: INSTALLATION},
            details_url=lambda run_id: f"https://dataproof.test/runs/{run_id}",
        )

    def claim(self) -> Claim:
        claim = self.store.claim("w1", lease=LEASE)
        assert claim is not None
        return claim

    def report(self, claim: Claim, report: ReportRecord) -> Claim:
        """Drive a claimed run on from its current stage to review, recording ``report``."""
        stages = [S.QUEUED, S.FETCHING, S.ANALYZING, S.PROVISIONING, S.MIGRATING, S.VERIFYING]
        for state in [*stages[stages.index(claim.state) + 1 :], S.REPORTING]:
            claim = self.store.advance(claim, state)
        return self.store.advance(
            claim,
            S.READY_FOR_REVIEW,
            effects=lambda conn: record_report(conn, claim.run_id, report),
        )


@pytest.fixture
def setup(store: RunStore, control_dsn: str) -> Setup:
    return Setup(store, control_dsn)


def test_a_check_run_follows_the_run_on_its_exact_head_commit(setup: Setup) -> None:
    run_id = create(setup.store).run.run_id
    assert setup.publisher.publish_pending() == [run_id]
    (check,) = setup.github.check_runs.values()
    assert (check.name, check.head_sha, check.external_id) == (CHECK_NAME, HEAD_SHA, str(run_id))
    assert check.details_url == f"https://dataproof.test/runs/{run_id}"
    assert check.status == "queued"

    claim = setup.store.advance(setup.claim(), S.FETCHING)
    assert setup.publisher.publish_pending() == [run_id]
    assert setup.publisher.publish_pending() == [], "nothing new to say"
    assert check.status == "in_progress"

    setup.report(claim, BLOCKED)
    assert setup.publisher.publish_pending() == [run_id]
    assert (check.status, check.conclusion) == ("completed", "failure")
    assert check.output["title"] == "BLOCKED: 1 required check failed or errored"
    summary = check.output["summary"]
    assert "| `commerce.c1_event_preservation` | yes | FAIL |  | 1 |" in summary
    assert "never change a check or the verdict" in summary
    assert HEAD_SHA in summary
    assert check.history == [("queued", None), ("in_progress", None), ("completed", "failure")]
    assert setup.publisher.publish_pending() == [], "a completed check run is final"
    assert len(setup.github.check_runs) == 1


@pytest.mark.parametrize(
    ("interrupt", "conclusion", "title"),
    [
        ("cancel", "cancelled", "Cancelled before it was verified"),
        ("supersede", "cancelled", "Superseded by a newer commit"),
        ("error", "failure", "Not verified: ERROR BASE_MIGRATION_FAILED"),
        ("timeout", "timed_out", "Timed out before it was verified"),
    ],
)
def test_a_run_interrupted_before_it_reported_is_never_green(
    setup: Setup, control_dsn: str, interrupt: str, conclusion: str, title: str
) -> None:
    run_id = create(setup.store).run.run_id
    claim = setup.store.advance(setup.claim(), S.FETCHING)
    if interrupt == "cancel":
        assert setup.store.cancel(run_id, actor="user:maintainer")
    elif interrupt == "supersede":
        create(setup.store, run_inputs(head_sha=NEW_HEAD_SHA))
    elif interrupt == "error":
        setup.store.advance(claim, S.ERROR, error_code=ErrorCode.BASE_MIGRATION_FAILED)
    else:
        pass_deadline(control_dsn, run_id)
        assert setup.store.expire_overdue(actor="reconciler") == [run_id]

    setup.publisher.publish(run_id)
    (check,) = setup.github.runs_on(HEAD_SHA)
    assert (check.status, check.conclusion) == ("completed", conclusion)
    assert check.output["title"] == title
    assert "not green" in check.output["summary"]


def test_a_check_run_created_before_a_crash_is_found_not_duplicated(setup: Setup) -> None:
    run_id = create(setup.store).run.run_id
    repository = setup.publisher.github.repository(INSTALLATION, REPOSITORY)
    # GitHub accepted the check run, but the publisher crashed before recording it.
    created = repository.create_check_run(
        {"name": CHECK_NAME, "head_sha": HEAD_SHA, "external_id": str(run_id), "status": "queued"}
    )
    setup.publisher.publish(run_id)
    assert [c.id for c in setup.github.runs_on(HEAD_SHA)] == [created]
    record = setup.records.check_run(run_id)
    assert record is not None
    assert record.check_run_id == created


def test_an_outage_leaves_the_status_to_publish_again(setup: Setup) -> None:
    run_id = create(setup.store).run.run_id
    setup.github.fail_next = [502]
    assert setup.publisher.publish_pending() == []
    assert setup.records.publications(run_id) == {}
    assert setup.publisher.publish_pending() == [run_id]
    assert setup.records.publications(run_id) == {"queued": None}


def test_runs_of_other_repositories_and_without_a_pull_request_are_not_published(
    setup: Setup,
) -> None:
    create(setup.store, run_inputs(repository="example-org/elsewhere"))
    create(setup.store, run_inputs(pull_request=None))
    assert setup.publisher.publish_pending() == []
    assert setup.github.check_runs == {}


def test_concurrent_publishers_create_one_check_run(setup: Setup) -> None:
    run_id = create(setup.store).run.run_id
    barrier = threading.Barrier(4)

    def publish(_: int) -> bool:
        barrier.wait()
        return setup.publisher.publish(run_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        sent = list(pool.map(publish, range(4)))
    assert sent.count(True) == 1
    assert len(setup.github.runs_on(HEAD_SHA)) == 1


def test_a_not_executed_run_is_reported_as_such(setup: Setup) -> None:
    run_id = create(setup.store).run.run_id
    claim = setup.store.advance(setup.claim(), S.FETCHING)
    claim = setup.store.advance(claim, S.ANALYZING)
    report = ReportRecord(
        verdict=ReportVerdict.NOT_EXECUTED,
        conclusion=GitHubCheckConclusion.NEUTRAL,
        report={"not_executed_reason": "FORK_PULL_REQUEST", "coverage": []},
    )
    setup.store.advance(
        claim,
        S.NOT_EXECUTED,
        not_executed_reason=NotExecutedReason.FORK_PULL_REQUEST,
        effects=lambda conn: record_report(conn, run_id, report),
    )
    setup.publisher.publish(run_id)
    (check,) = setup.github.runs_on(HEAD_SHA)
    assert (check.status, check.conclusion) == ("completed", "neutral")
    assert check.output["title"] == "NOT_EXECUTED: FORK_PULL_REQUEST"
    assert "No checks ran." in check.output["summary"]
