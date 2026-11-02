"""End to end on the local Docker backend: real runs of commerce change sets through the pipeline.

Each run is created in the control store, claimed by a worker and driven through real reference
and candidate environments, then cleaned up. The tests assert the recorded results, not logs.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from commerce_scenario import PUBLISHED_DIGEST
from control_env import FAST_RETRY, create, make_due
from dataproof_api import Intake, Registration
from dataproof_api.checks import CheckPublisher
from dataproof_api.github_app import GitHubApp, load_private_key
from dataproof_contracts import (
    ApproverSeparation,
    CheckOutcome,
    DecimalValue,
    Environment,
    ErrorCode,
    GitHubCheckConclusion,
    HumanDecision,
    HumanDecisionAction,
    IntegerValue,
    InvestigationStatus,
    NotExecutedReason,
    NotRunReason,
    ReportVerdict,
    ReproductionStatus,
    ReviewRun,
    RunInputs,
    RunState,
    RunTrigger,
    ToolCallOutcome,
    canonical_json,
)
from dataproof_control import GitHubRecords, RunResults, RunStore
from dataproof_investigator import Investigator, ScriptedModel, answer, call
from dataproof_runner import StageContext, StageOutcome, Worker, WorkerSettings
from dataproof_runner.artifacts import ArtifactIntegrityError, LocalArtifactStore
from dataproof_runner.baselines import CommerceBaseline
from dataproof_runner.configured import ConfiguredPipelines
from dataproof_runner.decisions import NotAwaitingReviewError, awaiting_review, decide_binding
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.pipeline import MIGRATION_CHECK, PREVIEW_CHECK, Pipeline, PipelineSettings
from dataproof_runner.project import ProjectConfig
from dataproof_verifier import parse_binding
from docker_env import BINDING_DIR, CHANGES, SAMPLE_APP, CommerceRepo, http_get, revision
from github_env import APP_ID, INSTALLATION, PRIVATE_KEY_PEM, FakeGitHub, pull_request_event

pytestmark = [pytest.mark.integration, pytest.mark.docker]
S = RunState
REPOSITORY = "example-org/commerce-demo"
SAFE, F1 = "ledger-backfill-01", "ledger-backfill-07"
C1, C2, C3 = (
    "commerce.c1_event_preservation",
    "commerce.c2_refund_totals",
    "commerce.c3_capture_totals",
)
C6_TOTALS, C6_REFUNDS = "commerce.c6_order_totals_api", "commerce.c6_refund_events_api"
SETTINGS = WorkerSettings(
    lease=timedelta(minutes=5),
    heartbeat_interval=timedelta(seconds=10),
    review_ttl=timedelta(hours=1),
    retry=FAST_RETRY,
)


@pytest.fixture(scope="session")
def commerce_repo(tmp_path_factory: pytest.TempPathFactory) -> CommerceRepo:
    """Data contracts only: runs are complete without previews."""
    return CommerceRepo(tmp_path_factory.mktemp("commerce-repo") / "repo")


@pytest.fixture(scope="session")
def commerce_api_repo(tmp_path_factory: pytest.TempPathFactory) -> CommerceRepo:
    """Data and API behaviour contracts: runs need both previews to be complete."""
    return CommerceRepo(tmp_path_factory.mktemp("commerce-api-repo") / "repo", api_oracles=True)


@dataclass
class Harness:
    store: RunStore
    dsn: str
    results: RunResults
    backend: LocalDockerBackend
    repo: CommerceRepo
    artifacts: LocalArtifactStore
    runtime_image: str

    def project(self, head: str | None = None, *, approve: bool = True) -> ProjectConfig:
        """The trusted configuration, approving exactly the bindings that ``head`` proposes."""
        approved = frozenset(self.binding_digests(head)) if head and approve else frozenset()
        return ProjectConfig(
            repository=REPOSITORY,
            runtime_image=self.runtime_image,
            baseline="commerce-v1",
            approved_binding_digests=approved,
            step_timeout_seconds=300,
        )

    def binding_digests(self, head: str) -> list[str]:
        digests = []
        for path in sorted((self.repo.path / BINDING_DIR).glob("*.toml")):
            name = f"{BINDING_DIR}/{path.name}"
            text = self.repo.show(head, name)
            digests.append(parse_binding(text, source_path=name, head_sha=head)[1].digest())
        return digests

    def pipeline(
        self,
        project: ProjectConfig,
        *,
        previews: bool = False,
        investigator: Investigator | None = None,
    ) -> Pipeline:
        """Previews are off unless a test is about them: each one builds a frontend."""
        return Pipeline(
            project=project,
            clone=self.repo.path,
            backend=self.backend,
            artifacts=self.artifacts,
            results=self.results,
            baseline=CommerceBaseline(),
            settings=PipelineSettings(previews=previews),
            investigator=investigator,
        )

    def configured(self) -> Worker:
        """A worker that runs each run under the stored configuration it pins."""
        pipelines = ConfiguredPipelines(self.store, self.backend, self.pipeline)
        return Worker(
            self.store, pipelines.handlers(), pipelines.cleanup, worker_id="w1", settings=SETTINGS
        )

    def worker(
        self, pipeline: Pipeline, **overrides: Callable[[StageContext], StageOutcome]
    ) -> Worker:
        replaced = {RunState[name.upper()]: handler for name, handler in overrides.items()}
        handlers = {**pipeline.handlers(), **replaced}
        return Worker(self.store, handlers, pipeline.cleanup, worker_id="w1", settings=SETTINGS)

    def submit(
        self,
        project: ProjectConfig,
        head: str,
        *,
        pull_request: int = 7,
        head_repository: str | None = None,
    ) -> UUID:
        inputs = RunInputs(
            repository=REPOSITORY,
            pull_request=pull_request,
            base_sha=self.repo.base,
            head_sha=head,
            head_repository=head_repository,
            config_digest=project.digest(),
            dataset_digest=PUBLISHED_DIGEST,
            engine_version="0.1.0",
        )
        return create(self.store, inputs).run.run_id

    def outcomes(self, run_id: UUID) -> dict[str, CheckOutcome]:
        return {c.result.check_id: c.result.outcome for c in self.results.checks(run_id)}

    def states(self, run_id: UUID) -> list[RunState]:
        return [t.to_state for t in self.store.transitions(run_id)]


@pytest.fixture
def harness(
    store: RunStore,
    control_dsn: str,
    backend: LocalDockerBackend,
    commerce_repo: CommerceRepo,
    runtime_image: str,
    tmp_path: Path,
) -> Harness:
    return Harness(
        store=store,
        dsn=control_dsn,
        results=RunResults(control_dsn),
        backend=backend,
        repo=commerce_repo,
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        runtime_image=runtime_image,
    )


@pytest.fixture
def api_harness(harness: Harness, commerce_api_repo: CommerceRepo) -> Harness:
    return replace(harness, repo=commerce_api_repo)


def test_a_safe_change_is_checked_survives_a_retry_and_is_cleaned_up(harness: Harness) -> None:
    head = harness.repo.change(SAFE)
    project = harness.project(head)
    pipeline = harness.pipeline(project)
    failures = iter([RuntimeError("verifier connection reset")])

    def flaky_verify(context: StageContext) -> StageOutcome:
        if (error := next(failures, None)) is not None:
            raise error
        return pipeline.verify(context)

    worker = harness.worker(pipeline, verifying=flaky_verify)
    run_id = harness.submit(project, head)

    assert worker.run_once()  # fails once in VERIFYING
    assert harness.store.get_run(run_id).state is S.VERIFYING
    assert worker.run_once()  # a new attempt re-attaches to both environments
    run = harness.store.get_run(run_id)
    assert run.state is S.READY_FOR_REVIEW
    assert run.isolation_id is not None
    assert run.isolation_id.startswith(f"dp-{harness.backend.scope}-")

    report = harness.results.report(run_id)
    assert report is not None
    assert (report.verdict, report.conclusion) == (
        ReportVerdict.CHECKED,
        GitHubCheckConclusion.SUCCESS,
    )
    assert [row["check_id"] for row in report.report["coverage"]] == [C1, C2, C3, MIGRATION_CHECK]
    assert harness.outcomes(run_id) == dict.fromkeys(
        [C1, C2, C3, MIGRATION_CHECK], CheckOutcome.PASS
    )
    assert report.report["head_sha"] == head
    assert report.report["backend"] == "local-docker"

    steps = {s.step_id: s for s in harness.results.steps(run_id)}
    assert set(steps) == {
        "reference.base-migration",
        "candidate.base-migration",
        "candidate.migration",
    }
    assert all(step.succeeded for step in steps.values())
    sources = harness.results.sources(run_id)
    assert steps["candidate.migration"].source_sha256 == sources[Environment.CANDIDATE].sha256
    assert steps["reference.base-migration"].source_sha256 == sources[Environment.REFERENCE].sha256
    provisioned = next(t for t in harness.store.transitions(run_id) if t.to_state is S.MIGRATING)
    assert provisioned.detail["dataset_digest"] == PUBLISHED_DIGEST
    assert len(harness.backend.resources(run_id)) == 6, "previews stay up; restore copies are gone"

    snapshots = harness.results.snapshots(run_id)
    assert set(snapshots) == set(Environment)
    assert all(s.restore_verified and s.restore_error is None for s in snapshots.values())
    sealed = harness.results.manifest(run_id)
    assert sealed is not None
    manifest = sealed.manifest
    assert harness.artifacts.get(sealed.object_key, sealed.sha256) == canonical_json(manifest)
    assert manifest.input_digest == harness.store.get_run(run_id).input_digest
    assert {s.sha256 for s in manifest.snapshots} == {s.sha256 for s in snapshots.values()}
    assert all(r.restored for r in manifest.restore_verifications)
    assert {a.sha256 for a in manifest.source_artifacts} == {s.sha256 for s in sources.values()}
    assert manifest.binding_digest is not None
    assert manifest.retain_until > manifest.sealed_at
    for record in manifest.records:
        harness.artifacts.get(record.object_key, record.sha256)  # present and unaltered
    assert report.report["evidence"]["manifest_sha256"] == sealed.sha256

    make_due(harness.dsn, run_id)
    assert worker.run_once()
    assert harness.states(run_id)[-3:] == [S.READY_FOR_REVIEW, S.CLEANING, S.COMPLETE]
    assert harness.states(run_id).count(S.VERIFYING) == 1, "a retry repeats the stage in place"
    assert harness.backend.resources(run_id) == []
    assert harness.store.transitions(run_id)[-1].detail["cleanup"] == "SUCCEEDED"


def test_f1_is_blocked_with_the_refund_evidence_and_reproduces_exactly(harness: Harness) -> None:
    head = harness.repo.change(F1)
    project = harness.project(head)
    pipeline = harness.pipeline(project)
    worker = harness.worker(pipeline)
    first = harness.submit(project, head, pull_request=7)
    second = harness.submit(project, head, pull_request=8)
    while worker.run_once():
        pass
    for run_id in (first, second):
        make_due(harness.dsn, run_id)
    while worker.run_once():
        pass

    for run_id in (first, second):
        report = harness.results.report(run_id)
        assert report is not None
        assert (report.verdict, report.conclusion) == (
            ReportVerdict.BLOCKED,
            GitHubCheckConclusion.FAILURE,
        )
    outcomes = harness.outcomes(first)
    assert outcomes[MIGRATION_CHECK] is CheckOutcome.PASS, "F1 migrates successfully"
    assert outcomes[C1] is CheckOutcome.FAIL
    (c1,) = [c.result for c in harness.results.checks(first) if c.result.check_id == C1]
    refund = [m for m in c1.mismatch_examples if m.column == "amount"]
    assert any(
        m.expected == DecimalValue(value=Decimal("49.99"))
        and m.observed == DecimalValue(value=Decimal("99.98"))
        for m in refund
    ), refund

    def fingerprint(run_id: UUID) -> list[Any]:
        checks = sorted(harness.results.checks(run_id), key=lambda c: c.result.check_id)
        evidence = harness.results.evidence(run_id)
        return [
            [
                (
                    c.result.check_id,
                    c.result.outcome,
                    c.result.mismatch_count,
                    c.result.mismatch_examples,
                )
                for c in checks
            ],
            {key: value.get("result_sha256") for key, value in evidence.items()},
        ]

    assert fingerprint(first) == fingerprint(second), "the same inputs give the same evidence"

    # The environments are gone, but the sealed candidate snapshot still shows the corruption.
    assert harness.backend.resources(first) == []
    snapshot = harness.results.snapshots(first)[Environment.CANDIDATE]
    dump = harness.artifacts.get(snapshot.object_key, snapshot.sha256)
    replay = harness.backend.provision(
        first,
        Environment.CANDIDATE,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        variant="diagnose",
    )
    try:
        harness.backend.restore_database(replay, dump)
        access = replay.database
        with psycopg.connect(access.conninfo(access.reader)) as conn:
            amount = conn.execute(
                "SELECT amount::text FROM ledger_entries WHERE source_event_id = 1002"
            ).fetchone()
        assert amount == ("99.98",)
    finally:
        harness.backend.destroy(first)

    # Replaying from the sealed snapshots alone reproduces every data contract exactly.
    sealed = pipeline.replay(harness.store.get_run(first))
    assert sealed.matches, sealed.differences
    assert set(sealed.replayed) == {C1, C2, C3}
    assert MIGRATION_CHECK in sealed.skipped
    assert harness.backend.resources(first) == [], "replay environments are destroyed"
    assert harness.store.get_run(first).state is S.COMPLETE, "a replay never changes the run"

    # Evidence that no longer matches its seal is refused, never replayed.
    tampered = dump[:-1] + bytes([dump[-1] ^ 0xFF])
    (harness.artifacts.root / snapshot.object_key).write_bytes(tampered)
    with pytest.raises(ArtifactIntegrityError):
        pipeline.replay(harness.store.get_run(first))
    assert harness.backend.resources(first) == []


def test_unapproved_bindings_leave_the_run_incomplete_but_analyzed(harness: Harness) -> None:
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "Ledger backfill, take two.\n"})
    project = harness.project(head, approve=False)
    worker = harness.worker(harness.pipeline(project))
    run_id = harness.submit(project, head)
    worker.run_once()

    report = harness.results.report(run_id)
    assert report is not None
    assert (report.verdict, report.conclusion) == (
        ReportVerdict.INCOMPLETE,
        GitHubCheckConclusion.ACTION_REQUIRED,
    )
    checks = {c.result.check_id: c for c in harness.results.checks(run_id)}
    assert checks[MIGRATION_CHECK].result.outcome is CheckOutcome.PASS
    for contract in (C1, C2, C3):
        assert checks[contract].result.not_run_reason is NotRunReason.AWAITING_BINDING_REVIEW
        assert checks[contract].binding_analysis is not None, "the reviewer sees what it reads"


def test_a_dependency_change_is_not_executed_and_nothing_is_provisioned(harness: Harness) -> None:
    lock = (harness.repo.path / SAMPLE_APP / "uv.lock").read_text("utf-8")
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/uv.lock": lock + "\n# pinned elsewhere\n"})
    project = harness.project(head)
    worker = harness.worker(harness.pipeline(project))
    run_id = harness.submit(project, head)
    worker.run_once()

    run = harness.store.get_run(run_id)
    assert run.state is S.NOT_EXECUTED
    assert run.not_executed_reason is NotExecutedReason.DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL
    report = harness.results.report(run_id)
    assert report is not None
    assert (report.verdict, report.conclusion) == (
        ReportVerdict.NOT_EXECUTED,
        GitHubCheckConclusion.NEUTRAL,
    )
    assert report.report["paths"] == [f"{SAMPLE_APP}/uv.lock"]
    assert harness.results.steps(run_id) == []
    assert harness.backend.resources(run_id) == []


def test_a_failing_migration_blocks_and_skips_the_contracts(harness: Harness) -> None:
    bindings = {
        f"{BINDING_DIR}/{p.name}": p.read_text("utf-8")
        for p in sorted((CHANGES / SAFE / "bindings").glob("*.toml"))
    }
    broken = revision(
        "7c8d9e0f1a2b",
        '    op.execute("CREATE TABLE ledger_entries (id integer)")\n'
        '    op.execute("SELECT 1 / 0")',
    )
    head = harness.repo.commit(
        harness.repo.base,
        {f"{SAMPLE_APP}/migrations/versions/7c8d9e0f1a2b_broken.py": broken, **bindings},
        "A migration that fails",
    )
    project = harness.project(head)
    worker = harness.worker(harness.pipeline(project))
    run_id = harness.submit(project, head)
    worker.run_once()

    report = harness.results.report(run_id)
    assert report is not None
    assert report.verdict is ReportVerdict.BLOCKED
    checks = {c.result.check_id: c.result for c in harness.results.checks(run_id)}
    assert checks[MIGRATION_CHECK].outcome is CheckOutcome.FAIL
    for contract in (C1, C2, C3):
        assert checks[contract].not_run_reason is NotRunReason.PREREQUISITE_FAILED
    (step,) = [s for s in harness.results.steps(run_id) if s.step_id == "candidate.migration"]
    assert "division by zero" in step.output


def test_cancelling_during_migration_discards_its_result_and_leaves_nothing(
    harness: Harness,
) -> None:
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "Cancelled while migrating.\n"})
    project = harness.project(head)
    pipeline = harness.pipeline(project)

    def migrate_then_get_cancelled(context: StageContext) -> StageOutcome:
        outcome = pipeline.migrate(context)
        harness.store.cancel(context.run.run_id, actor="user:alice")
        return outcome

    worker = harness.worker(pipeline, migrating=migrate_then_get_cancelled)
    run_id = harness.submit(project, head)
    worker.run_once()
    assert harness.store.get_run(run_id).state is S.CANCELLED
    assert "candidate.migration" not in {s.step_id for s in harness.results.steps(run_id)}
    assert harness.backend.resources(run_id) != []

    worker.run_once()
    assert harness.states(run_id)[-3:] == [S.CANCELLED, S.CLEANING, S.COMPLETE]
    assert harness.backend.resources(run_id) == []
    assert harness.results.report(run_id) is None


def test_a_fork_pull_request_is_not_executed_and_nothing_is_provisioned(harness: Harness) -> None:
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "From a fork.\n"})
    project = harness.project(head)
    run_id = harness.submit(project, head, head_repository="someone-else/commerce-demo")
    harness.worker(harness.pipeline(project)).run_once()

    run = harness.store.get_run(run_id)
    assert run.state is S.NOT_EXECUTED
    assert run.not_executed_reason is NotExecutedReason.FORK_PULL_REQUEST
    report = harness.results.report(run_id)
    assert report is not None
    assert (report.verdict, report.conclusion) == (
        ReportVerdict.NOT_EXECUTED,
        GitHubCheckConclusion.NEUTRAL,
    )
    assert S.PROVISIONING not in harness.states(run_id)
    assert harness.backend.resources(run_id) == []


def test_a_run_pinned_to_another_configuration_is_not_run_under_this_one(
    harness: Harness,
) -> None:
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "Configuration drift.\n"})
    pinned = harness.project(head, approve=False)
    current = harness.project(head)
    worker = harness.worker(harness.pipeline(current))
    run_id = harness.submit(pinned, head)
    worker.run_once()
    run = harness.store.get_run(run_id)
    assert run.state is S.COMPLETE
    assert run.error_code is ErrorCode.CONFIG_DIGEST_MISMATCH
    assert harness.results.sources(run_id) == {}
    assert harness.backend.resources(run_id) == []


def test_a_pull_request_cannot_weaken_the_oracle_it_is_held_to(harness: Harness) -> None:
    """F1 plus an edit that makes C1 optional in the head: the base oracle still blocks it."""
    oracle = "dataproof/oracles/c1_event_preservation.toml"
    original = harness.repo.show(harness.repo.base, oracle)
    assert "required = true" in original
    head = harness.repo.change(
        F1, {oracle: original.replace("required = true", "required = false")}
    )
    project = harness.project(head)
    worker = harness.worker(harness.pipeline(project))
    run_id = harness.submit(project, head)
    worker.run_once()

    report = harness.results.report(run_id)
    assert report is not None
    assert report.verdict is ReportVerdict.BLOCKED
    (c1,) = [c.result for c in harness.results.checks(run_id) if c.result.check_id == C1]
    assert c1.required
    assert c1.outcome is CheckOutcome.FAIL


def test_a_new_head_supersedes_a_run_in_review_which_ends_superseded(harness: Harness) -> None:
    first_head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "First head.\n"})
    lock = (harness.repo.path / SAMPLE_APP / "uv.lock").read_text("utf-8")
    second_head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/uv.lock": lock + "\n# second head\n"})
    project = harness.project(first_head)
    worker = harness.worker(harness.pipeline(project))
    first = harness.submit(project, first_head)
    worker.run_once()
    assert harness.store.get_run(first).state is S.READY_FOR_REVIEW

    second = harness.submit(project, second_head)
    assert harness.store.get_run(first).superseded_by == second
    while worker.run_once():
        pass

    ended = harness.store.get_run(first)
    assert ended.state is S.SUPERSEDED
    assert harness.states(first)[-4:] == [S.READY_FOR_REVIEW, S.CANCELLED, S.CLEANING, S.SUPERSEDED]
    assert harness.backend.resources(first) == []
    assert harness.store.get_run(second).state is S.NOT_EXECUTED


def test_previews_serve_each_commit_until_the_run_is_cleaned_up(api_harness: Harness) -> None:
    harness = api_harness
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "With previews.\n"})
    project = harness.project(head)
    worker = harness.worker(harness.pipeline(project, previews=True))
    run_id = harness.submit(project, head)
    worker.run_once()

    report = harness.results.report(run_id)
    assert report is not None
    assert report.verdict is ReportVerdict.CHECKED
    outcomes = harness.outcomes(run_id)
    assert outcomes[PREVIEW_CHECK] is CheckOutcome.PASS
    assert outcomes[C6_TOTALS] is outcomes[C6_REFUNDS] is CheckOutcome.PASS, "API behaviour kept"
    c6 = {c.result.check_id: c.result for c in harness.results.checks(run_id)}
    assert c6[C6_TOTALS].expected_key_count == c6[C6_TOTALS].observed_key_count == 208
    previews = harness.results.previews(run_id)
    commits = {Environment.REFERENCE: harness.repo.base, Environment.CANDIDATE: head}
    for role, preview in previews.items():
        assert preview.ready
        assert preview.commit_sha == commits[role]
        assert preview.build_seconds is not None
        assert preview.build_seconds > 0
        assert preview.peak_memory_bytes > 0
    assert set(previews) == set(Environment)

    accesses = {}
    for role in Environment:
        handle = harness.backend.attach(run_id, role)
        assert handle is not None
        access = harness.backend.preview_access(handle)
        assert access is not None
        assert hashlib.sha256(access.token.encode()).hexdigest() == previews[role].token_sha256
        assert access.token not in json.dumps(report.report), "tokens never enter the report"
        accesses[role] = access

    reference, candidate = accesses[Environment.REFERENCE], accesses[Environment.CANDIDATE]
    status, headers, body = http_get(f"{reference.url}/api/orders/1/refunds", reference.token)
    assert status == 200
    assert headers["X-DataProof-Commit"] == harness.repo.base
    assert [(r["event_id"], r["amount"]) for r in json.loads(body)] == [(1002, "49.99")]
    status, headers, body = http_get(
        f"{candidate.url}/api/orders/1/transactions?kind=REFUND", candidate.token
    )
    assert status == 200
    assert headers["X-DataProof-Commit"] == head
    assert [(t["source_event_id"], t["amount"]) for t in json.loads(body)] == [(1002, "49.99")]
    for access in (reference, candidate):
        status, _, page = http_get(f"{access.url}/orders/1", access.token)
        assert status == 200
        assert b"49.99" in page
        assert http_get(f"{access.url}/orders/1")[0] == 401

    make_due(harness.dsn, run_id)
    worker.run_once()
    assert harness.store.get_run(run_id).state is S.COMPLETE
    assert harness.backend.resources(run_id) == []


def test_a_candidate_frontend_that_does_not_build_blocks_the_run(api_harness: Harness) -> None:
    harness = api_harness
    head = harness.repo.change(
        SAFE, {f"{SAMPLE_APP}/web/lib/broken.ts": 'export const total: number = "x";\n'}
    )
    project = harness.project(head)
    worker = harness.worker(harness.pipeline(project, previews=True))
    run_id = harness.submit(project, head)
    worker.run_once()

    report = harness.results.report(run_id)
    assert report is not None
    assert report.verdict is ReportVerdict.BLOCKED
    (check,) = [
        c.result for c in harness.results.checks(run_id) if c.result.check_id == PREVIEW_CHECK
    ]
    assert (check.outcome, check.error_code) == (
        CheckOutcome.ERROR,
        ErrorCode.CANDIDATE_BUILD_FAILED,
    )
    outcomes = harness.outcomes(run_id)
    assert outcomes[C1] is CheckOutcome.PASS, "data checks still ran"
    checks = {c.result.check_id: c.result for c in harness.results.checks(run_id)}
    for api_check in (C6_TOTALS, C6_REFUNDS):
        assert checks[api_check].not_run_reason is NotRunReason.PREREQUISITE_FAILED
    candidate = harness.results.previews(run_id)[Environment.CANDIDATE]
    assert (candidate.ready, candidate.failure, candidate.url) == (False, "build", None)


def test_f1_is_also_blocked_by_api_behaviour_through_the_previews(api_harness: Harness) -> None:
    harness = api_harness
    head = harness.repo.change(F1, {f"{SAMPLE_APP}/README.md": "F1 with previews.\n"})
    project = harness.project(head)
    worker = harness.worker(harness.pipeline(project, previews=True))
    run_id = harness.submit(project, head)
    worker.run_once()

    report = harness.results.report(run_id)
    assert report is not None
    assert report.verdict is ReportVerdict.BLOCKED
    checks = {c.result.check_id: c.result for c in harness.results.checks(run_id)}
    assert checks[PREVIEW_CHECK].outcome is CheckOutcome.PASS, "the app itself works"
    assert checks[C6_TOTALS].outcome is CheckOutcome.FAIL
    assert checks[C6_REFUNDS].outcome is CheckOutcome.FAIL
    refunds = [
        (m.expected, m.observed)
        for m in checks[C6_REFUNDS].mismatch_examples
        if m.column == "amount" and m.key[1] == IntegerValue(value=1002)
    ]
    assert refunds == [(DecimalValue(value=Decimal("49.99")), DecimalValue(value=Decimal("99.98")))]
    evidence = harness.results.evidence(run_id)
    assert evidence[f"{C6_REFUNDS}:candidate:api"]["query_sql"] == (
        "GET /orders/{order_id}/transactions?kind=REFUND"
    )


def test_failures_are_investigated_but_only_ever_advised_on(harness: Harness) -> None:
    head = harness.repo.change(F1, {f"{SAMPLE_APP}/README.md": "Investigated.\n"})
    project = harness.project(head)
    binding_evidence = f"{C1}:candidate:binding"
    careful = ScriptedModel(
        [
            call(
                "run_readonly_query",
                {
                    "environment": "CANDIDATE",
                    "sql": "SELECT source_event_id, amount FROM ledger_entries WHERE order_id = 1",
                    "max_rows": 10,
                },
            ),
            answer(
                json.dumps(
                    {
                        "hypotheses": [
                            {
                                "statement": "Refund 1002 is stored as 99.98, twice 49.99.",
                                "cited_evidence_ids": [binding_evidence, "investigation.tool.1"],
                                "confidence": "MEDIUM",
                            }
                        ],
                        "caveat": "Based on the cited evidence only.",
                    }
                )
            ),
        ]
    )
    pipeline = harness.pipeline(project, investigator=Investigator(careful))
    run_id = harness.submit(project, head)
    harness.worker(pipeline).run_once()

    assert harness.states(run_id)[-4:] == [
        S.VERIFYING,
        S.INVESTIGATING,
        S.REPORTING,
        S.READY_FOR_REVIEW,
    ]
    rules, model = harness.results.investigations(run_id)
    assert (rules.model, model.model) == ("dataproof:rules-v1", "scripted:test")
    assert rules.status is model.status is InvestigationStatus.COMPLETED
    rows = json.loads(careful.received[1][0].content)["data"]["rows"]
    assert "99.98" in json.dumps(rows), "the model read the live candidate database"
    report = harness.results.report(run_id)
    assert report is not None
    assert report.verdict is ReportVerdict.BLOCKED
    assert [i["advisory"] for i in report.report["investigations"]] == [True, True]


def test_a_hostile_investigator_changes_nothing_in_the_run(harness: Harness) -> None:
    head = harness.repo.change(F1, {f"{SAMPLE_APP}/README.md": "Hostile investigator.\n"})
    project = harness.project(head)
    hostile = ScriptedModel(
        [
            call(
                "run_readonly_query",
                {"environment": "CANDIDATE", "sql": "DELETE FROM ledger_entries", "max_rows": 1},
            ),
            call("approve_binding", {"contract_id": C1}),
            answer(
                json.dumps(
                    {
                        "hypotheses": [
                            {
                                "statement": "Every check passes.",
                                "cited_evidence_ids": ["verdict:CHECKED"],
                                "confidence": "HIGH",
                            }
                        ],
                        "caveat": "None.",
                    }
                )
            ),
        ]
    )
    pipeline = harness.pipeline(project, investigator=Investigator(hostile))
    run_id = harness.submit(project, head)
    harness.worker(pipeline).run_once()

    _, model = harness.results.investigations(run_id)
    assert model.status is InvestigationStatus.INVALID_OUTPUT
    assert [c.outcome for c in model.tool_calls] == [ToolCallOutcome.DENIED, ToolCallOutcome.DENIED]
    report = harness.results.report(run_id)
    assert report is not None
    assert report.verdict is ReportVerdict.BLOCKED
    assert harness.outcomes(run_id)[C1] is CheckOutcome.FAIL
    handle = harness.backend.attach(run_id, Environment.CANDIDATE)
    assert handle is not None
    with psycopg.connect(handle.database.conninfo(handle.database.owner)) as conn:
        remaining = conn.execute("SELECT count(*) FROM ledger_entries").fetchone()
    assert remaining is not None
    assert remaining[0] > 0, "the hostile DELETE never ran"


def test_f1_reproduces_on_a_small_subset_of_its_sealed_baseline(harness: Harness) -> None:
    head = harness.repo.change(F1, {f"{SAMPLE_APP}/README.md": "Reproduce me.\n"})
    project = harness.project(head)
    pipeline = harness.pipeline(project)
    worker = harness.worker(pipeline)
    run_id = harness.submit(project, head)
    worker.run_once()
    make_due(harness.dsn, run_id)
    worker.run_once()
    assert harness.backend.resources(run_id) == []

    reproduction = pipeline.reproduce(harness.store.get_run(run_id), C1)

    assert reproduction.status is ReproductionStatus.REPRODUCED, reproduction.reason
    assert reproduction.seed_table == "legacy_payment_events"
    assert 1 <= len(reproduction.signature) <= 3
    rows = {t.table: t.rows for t in reproduction.subset}
    assert set(rows) == {"customers", "orders", "order_items", "legacy_payment_events"}
    assert sum(rows.values()) < 100, "a small subset of the 1172-row baseline"
    assert reproduction.result is not None
    assert reproduction.result.outcome is CheckOutcome.FAIL
    stored = harness.artifacts.get(reproduction.object_key or "", reproduction.subset_sha256 or "")
    assert stored, "the subset itself is stored for anyone to rerun"
    assert harness.results.reproductions(run_id) == [reproduction]
    assert harness.backend.resources(run_id) == []
    assert harness.store.get_run(run_id).state is S.COMPLETE


def test_a_failure_the_subset_cannot_show_is_honestly_not_reproduced(harness: Harness) -> None:
    """A fault that only corrupts data when the table is large does not recur on a subset."""
    (safe_revision,) = (CHANGES / SAFE).glob("*.py")
    size_dependent = (
        "    op.execute(\n"
        "        \"UPDATE ledger_entries SET amount = amount * 2 WHERE entry_kind = 'REFUND' \"\n"
        '        "AND (SELECT count(*) FROM legacy_payment_events) > 100"\n'
        "    )\n"
    )
    marker = "\n\ndef downgrade() -> None:"
    text = safe_revision.read_text("utf-8").replace(marker, "\n" + size_dependent + marker)
    head = harness.repo.change(
        SAFE, {f"{SAMPLE_APP}/migrations/versions/{safe_revision.name}": text}
    )
    project = harness.project(head)
    pipeline = harness.pipeline(project)
    run_id = harness.submit(project, head)
    harness.worker(pipeline).run_once()
    assert harness.outcomes(run_id)[C1] is CheckOutcome.FAIL

    reproduction = pipeline.reproduce(harness.store.get_run(run_id), C1)

    assert reproduction.status is ReproductionStatus.NOT_REPRODUCED
    assert reproduction.reason == "the check is PASS on the subset"
    assert reproduction.result is not None
    assert reproduction.result.outcome is CheckOutcome.PASS


def test_only_failed_checks_can_be_reproduced(harness: Harness) -> None:
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "Nothing to reproduce.\n"})
    project = harness.project(head)
    pipeline = harness.pipeline(project)
    run_id = harness.submit(project, head)
    harness.worker(pipeline).run_once()
    with pytest.raises(ValueError, match="did not fail"):
        pipeline.reproduce(harness.store.get_run(run_id), C1)


def test_a_correction_is_evaluated_as_a_new_run_and_the_original_stands(harness: Harness) -> None:
    head = harness.repo.change(F1, {f"{SAMPLE_APP}/README.md": "Needs a correction.\n"})
    project = harness.project(head)
    original = harness.submit(project, head)
    harness.worker(harness.pipeline(project)).run_once()
    report_before = harness.results.report(original)
    checks_before = harness.results.checks(original)
    assert report_before is not None
    assert report_before.verdict is ReportVerdict.BLOCKED

    # The developer pushes a correction: the faulty revision is replaced by the safe one.
    (faulty,) = (CHANGES / F1).glob("*.py")
    (fixed,) = (CHANGES / SAFE).glob("*.py")
    versions = f"{SAMPLE_APP}/migrations/versions"
    correction = harness.repo.commit(
        head,
        {f"{versions}/{faulty.name}": None, f"{versions}/{fixed.name}": fixed.read_text("utf-8")},
        "Aggregate events before the ledger insert",
    )
    corrected_project = harness.project(correction)
    created = harness.store.start_correction_replay(
        original,
        head_sha=correction,
        config_digest=corrected_project.digest(),
        actor="user:alice",
        run_ttl=timedelta(minutes=30),
        retry=FAST_RETRY,
    )
    harness.worker(harness.pipeline(corrected_project)).run_once()

    corrected = harness.results.report(created.run.run_id)
    assert corrected is not None
    assert corrected.verdict is ReportVerdict.CHECKED
    assert created.run.parent_run_id == original
    assert harness.results.report(original) == report_before, "the original report stands"
    assert harness.results.checks(original) == checks_before
    assert harness.store.get_run(original).superseded_by is None


def test_an_approval_runs_the_change_again_and_the_original_stays_incomplete(
    harness: Harness,
) -> None:
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "Awaiting binding review.\n"})
    project = harness.project(head, approve=False)
    harness.store.save_config(project.stored())
    worker = harness.configured()
    original = harness.submit(project, head)
    worker.run_once()
    report_before = harness.results.report(original)
    assert report_before is not None
    assert report_before.verdict is ReportVerdict.INCOMPLETE

    awaiting = awaiting_review(harness.results, original)
    proposed = {c.result.check_id: c.binding for c in harness.results.checks(original)}
    bindings = [b for c in (C1, C2, C3) if (b := proposed[c]) is not None]
    assert len(bindings) == 3
    assert {b.digest() for b in bindings} == set(awaiting), "each check waits on its binding"
    with pytest.raises(NotAwaitingReviewError):
        decide_binding(
            harness.store,
            harness.results,
            _decision(harness.store.get_run(original), "0" * 64),
            run_ttl=timedelta(minutes=30),
            retry=FAST_RETRY,
        )

    started = []
    for digest in sorted(awaiting):
        outcome = decide_binding(
            harness.store,
            harness.results,
            _decision(harness.store.get_run(original), digest),
            run_ttl=timedelta(minutes=30),
            retry=FAST_RETRY,
        )
        assert outcome.started is not None
        started.append(outcome.started.run)
    # Each approval starts a run; only the last carries all three, so the others are cancelled.
    for run in started[:-1]:
        assert harness.store.cancel(run.run_id, actor="user:maintainer")
    while worker.run_once():
        pass

    approved = started[-1]
    assert approved.trigger is RunTrigger.BINDING_APPROVAL
    assert approved.parent_run_id == original
    report = harness.results.report(approved.run_id)
    assert report is not None
    assert (report.verdict, report.conclusion) == (
        ReportVerdict.CHECKED,
        GitHubCheckConclusion.SUCCESS,
    )
    assert {harness.outcomes(approved.run_id)[c] for c in (C1, C2, C3)} == {CheckOutcome.PASS}
    assert harness.results.report(original) == report_before, "the original is never resumed"
    assert harness.store.get_run(original).superseded_by is None
    assert [d.binding_digest for d in harness.store.decisions(original)] == sorted(awaiting)


def test_a_run_whose_configuration_is_not_stored_is_an_error(harness: Harness) -> None:
    head = harness.repo.change(SAFE, {f"{SAMPLE_APP}/README.md": "Unknown configuration.\n"})
    run_id = harness.submit(harness.project(head), head)
    harness.configured().run_once()

    run = harness.store.get_run(run_id)
    assert run.state is S.COMPLETE
    assert run.error_code is ErrorCode.CONFIG_DIGEST_MISMATCH
    assert S.FETCHING in harness.states(run_id)
    assert harness.results.sources(run_id) == {}, "nothing ran under another configuration"


def _decision(run: ReviewRun, digest: str) -> HumanDecision:
    return HumanDecision(
        decision_id=uuid4(),
        run_id=run.run_id,
        head_sha=run.inputs.head_sha,
        action=HumanDecisionAction.APPROVE_BINDING,
        actor="maintainer",
        decided_at=datetime.now(UTC),
        binding_digest=digest,
        pr_author="developer",
        approver_separation=ApproverSeparation.ORGANIZATIONAL,
    )


def test_a_signed_pull_request_event_ends_as_a_failing_check_on_its_head_commit(
    harness: Harness,
) -> None:
    """GitHub event to commit-bound check, through intake, the worker and the publisher."""
    head = harness.repo.change(F1, {f"{SAMPLE_APP}/README.md": "Opened on GitHub.\n"})
    records = GitHubRecords(harness.dsn)
    registration = Registration(
        project=harness.project(head),
        installation_id=INSTALLATION,
        dataset_digest=PUBLISHED_DIGEST,
        engine_version="0.1.0",
    )
    intake = Intake(
        harness.store,
        records,
        {REPOSITORY: registration},
        run_ttl=timedelta(minutes=30),
        retry=FAST_RETRY,
    )
    github = FakeGitHub({INSTALLATION: {REPOSITORY}})
    publisher = CheckPublisher(
        harness.store,
        harness.results,
        records,
        GitHubApp(APP_ID, load_private_key(PRIVATE_KEY_PEM), client=github.client()),
        {REPOSITORY: INSTALLATION},
    )

    received = intake.receive(
        "delivery-1", "pull_request", pull_request_event(head=head, base=harness.repo.base)
    )
    assert received.run_id is not None
    publisher.publish_pending()
    harness.configured().run_once()
    publisher.publish_pending()

    (check,) = github.runs_on(head)
    assert check.external_id == str(received.run_id)
    assert check.history == [("queued", None), ("completed", "failure")]
    assert check.output["title"].startswith("BLOCKED")
    assert f"| `{C1}` | yes | FAIL |" in check.output["summary"]
    assert github.runs_on(harness.repo.base) == [], "only the head commit is reported on"
