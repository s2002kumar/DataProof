"""Commit-bound check runs, published from the control database to a fake GitHub."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_env import (
    FAST_RETRY,
    HEAD_SHA,
    LEASE,
    NEW_HEAD_SHA,
    REPOSITORY,
    RUN_TTL,
    create,
    pass_deadline,
    run_inputs,
)
from dataproof_api.checks import CHECK_NAME, CheckPublisher
from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_contracts import (
    ApproverSeparation,
    ErrorCode,
    GitHubCheckConclusion,
    HumanDecision,
    HumanDecisionAction,
    NotExecutedReason,
    ReportVerdict,
    ReviewRun,
    RunState,
    RunTrigger,
)
from dataproof_control import (
    Claim,
    GitHubRecords,
    ReportRecord,
    RunResults,
    RunStore,
    StoredConfig,
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
                "expected_key_count": 1207,
                "observed_key_count": 1207,
                "examples": [
                    {
                        "key": [{"type": "integer", "value": 1002}],
                        "column": "amount",
                        "expected": {"type": "decimal", "value": "49.99"},
                        "observed": {"type": "decimal", "value": "99.98"},
                    }
                ],
            },
        ],
        "investigations": [
            {
                "model": "dataproof:rules-v1",
                "status": "COMPLETED",
                "hypotheses": [
                    {
                        "statement": "commerce.c1_event_preservation: Every changed value is "
                        "an exact multiple (2x) of the reference value: consistent with a join "
                        "that repeats each row before it is summed or copied.",
                        "cited_evidence_ids": ["commerce.c1_event_preservation:candidate:binding"],
                        "confidence": "MEDIUM",
                    }
                ],
            }
        ],
        "evidence": {
            "manifest_sha256": "a" * 64,
            "snapshots": {
                "REFERENCE": {"sha256": "b" * 64, "restore_verified": True},
                "CANDIDATE": {"sha256": "c" * 64, "restore_verified": True},
            },
        },
    },
)

INCOMPLETE = ReportRecord(
    verdict=ReportVerdict.INCOMPLETE,
    conclusion=GitHubCheckConclusion.ACTION_REQUIRED,
    report={
        "coverage": [
            {"check_id": "commerce.c1_event_preservation", "required": True, "outcome": "NOT_RUN"}
        ]
    },
)
CHECKED = ReportRecord(
    verdict=ReportVerdict.CHECKED,
    conclusion=GitHubCheckConclusion.SUCCESS,
    report={
        "coverage": [
            {"check_id": "commerce.c1_event_preservation", "required": True, "outcome": "PASS"}
        ]
    },
)


def approve(store: RunStore, run: ReviewRun, binding: str) -> UUID:
    """A maintainer approves one binding on ``run``; the approval run that started."""
    decision = HumanDecision(
        decision_id=uuid4(),
        run_id=run.run_id,
        head_sha=run.inputs.head_sha,
        action=HumanDecisionAction.APPROVE_BINDING,
        actor="maintainer",
        decided_at=datetime.now(UTC),
        binding_digest=hashlib.sha256(binding.encode()).hexdigest(),
        pr_author="author",
        approver_separation=ApproverSeparation.ORGANIZATIONAL,
    )

    def configure(approved: frozenset[str], rejected: frozenset[str]) -> StoredConfig:
        config = {
            "repository": REPOSITORY,
            "approved": sorted(approved),
            "rejected": sorted(rejected),
        }
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        return StoredConfig(digest=digest, repository=REPOSITORY, config=config)

    recorded = store.decide_binding(
        decision, configure=configure, run_ttl=RUN_TTL, retry=FAST_RETRY
    )
    assert recorded.started is not None
    return recorded.started.run.run_id


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
    assert summary.startswith("## BLOCKED (`failure`)")
    assert "1 required check failed" in summary
    assert "**Next:** Open the run for every mismatch" in summary
    assert "| 1002 | `amount` | 49.99 | 99.98 |" in summary, "expected versus observed"
    assert "| `commerce.c1_event_preservation` | yes | FAIL |  | 1,207 | 1 |" in summary
    assert "Required: 1 passed, 1 failed, 0 errored, 0 not run." in summary
    assert "evidence manifest `aaaaaaaaaaaa`" in summary
    assert "reference snapshot `bbbbbbbbbbbb` (restore verified)" in summary
    assert "### Rule-based diagnosis (advisory)" in summary
    assert "exact multiple (2x)" in summary
    assert "never change a check or the verdict" in summary
    assert HEAD_SHA in summary
    assert check.history == [("queued", None), ("in_progress", None), ("completed", "failure")]
    assert setup.publisher.publish_pending() == [], "a completed check run is final"
    assert len(setup.github.check_runs) == 1


@pytest.mark.parametrize(
    ("interrupt", "conclusion", "title"),
    [
        ("cancel", "cancelled", "Cancelled before it was verified"),
        ("supersede", "cancelled", "Superseded by a newer run"),
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


def test_a_burst_of_approvals_leaves_the_newest_run_as_the_commits_check(setup: Setup) -> None:
    """The live demo's commit 2e49a55: approvals in quick succession start approval runs, each
    superseding the queued one before it, between two passes of the publisher. A superseded run
    that never reached GitHub gets no check run: one created after its successor's would be the
    check run a required check reads, and that blocked a commit DataProof had checked."""
    original = create(setup.store).run
    setup.report(setup.claim(), INCOMPLETE)
    assert setup.publisher.publish_pending() == [original.run_id]
    started = [approve(setup.store, original, binding) for binding in ("b1", "b2", "b3")]
    *superseded, newest = started
    assert [setup.store.get_run(run_id).superseded_by for run_id in superseded] == started[1:]

    assert setup.publisher.publish_pending() == [newest]
    assert setup.publisher.publish_pending() == [], "nothing is left to publish or decide"
    assert [c.external_id for c in setup.github.runs_on(HEAD_SHA)] == [
        str(original.run_id),
        str(newest),
    ]
    for run_id in superseded:
        assert setup.records.check_run(run_id) is None
        assert setup.records.omission(run_id) == newest

    while (claim := setup.claim()).run_id != newest:
        pass  # the superseded runs are claimed first, to be cleaned up
    setup.report(claim, CHECKED)
    setup.publisher.publish_pending()
    latest = setup.github.latest(HEAD_SHA)
    assert latest is not None
    assert (latest.external_id, latest.conclusion) == (str(newest), "success")


def test_an_older_runs_success_never_hides_a_newer_run_on_the_commit(setup: Setup) -> None:
    """Fail-open, closed: an older run reported CHECKED while the publisher was behind, and a
    newer run on the same commit was published first. Created after it, the older run's green
    check run would be the one a required check reads, over the newer run's BLOCKED."""
    older = create(setup.store).run
    setup.report(setup.claim(), CHECKED)
    newer = create(setup.store, run_inputs(config_digest="c" * 64), trigger=RunTrigger.MANUAL).run
    assert setup.store.cancel(older.run_id, actor="user:maintainer"), "the older run changes last"

    setup.publisher.publish_pending()
    setup.report(setup.claim(), BLOCKED)
    setup.publisher.publish_pending()
    latest = setup.github.latest(HEAD_SHA)
    assert latest is not None
    assert (latest.external_id, latest.conclusion) == (str(newer.run_id), "failure")
    assert setup.records.check_run(older.run_id) is None
    assert setup.records.omission(older.run_id) == newer.run_id


def test_a_run_already_shown_on_github_is_completed_after_a_newer_run(setup: Setup) -> None:
    older = create(setup.store).run
    repository = setup.publisher.github.repository(INSTALLATION, REPOSITORY)
    # GitHub accepted the older run's check run, but the publisher crashed before recording it.
    orphan = repository.create_check_run(
        {"name": CHECK_NAME, "head_sha": HEAD_SHA, "external_id": str(older.run_id)}
        | {"status": "queued"}
    )
    newer = create(setup.store, run_inputs(config_digest="c" * 64), trigger=RunTrigger.MANUAL)
    assert setup.store.cancel(older.run_id, actor="user:maintainer")

    setup.publisher.publish_pending()
    assert (setup.github.check_runs[orphan].status, setup.github.check_runs[orphan].conclusion) == (
        "completed",
        "cancelled",
    ), "a check run GitHub shows is never left open"
    latest = setup.github.latest(HEAD_SHA)
    assert latest is not None
    assert latest.external_id == str(newer.run.run_id)


def test_check_runs_on_one_commit_are_published_one_at_a_time(setup: Setup) -> None:
    run_id = create(setup.store).run.run_id
    elsewhere = create(setup.store, run_inputs(head_sha=NEW_HEAD_SHA, pull_request=8)).run.run_id
    with setup.records.publishing(REPOSITORY, HEAD_SHA) as held:
        assert held
        assert not setup.publisher.publish(run_id), "another publisher holds the commit"
        assert setup.publisher.publish(elsewhere), "other commits are not held up"
    assert setup.publisher.publish(run_id)


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
        conclusion=GitHubCheckConclusion.ACTION_REQUIRED,
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
    assert (check.status, check.conclusion) == ("completed", "action_required")
    assert check.output["title"] == "NOT_EXECUTED: FORK_PULL_REQUEST"
    assert "No checks ran." in check.output["summary"]
