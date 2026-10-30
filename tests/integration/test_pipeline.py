"""End to end on the local Docker backend: real runs of commerce change sets through the pipeline.

Each run is created in the control store, claimed by a worker and driven through real reference
and candidate environments, then cleaned up. The tests assert the recorded results, not logs.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from commerce_scenario import PUBLISHED_DIGEST
from control_env import FAST_RETRY, create, make_due
from dataproof_contracts import (
    CheckOutcome,
    DecimalValue,
    Environment,
    ErrorCode,
    GitHubCheckConclusion,
    NotExecutedReason,
    NotRunReason,
    ReportVerdict,
    RunInputs,
    RunState,
)
from dataproof_control import RunResults, RunStore
from dataproof_runner import StageContext, StageOutcome, Worker, WorkerSettings
from dataproof_runner.artifacts import LocalArtifactStore
from dataproof_runner.baselines import CommerceBaseline
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.pipeline import MIGRATION_CHECK, Pipeline
from dataproof_runner.project import ProjectConfig
from dataproof_verifier import parse_binding
from docker_env import BINDING_DIR, CHANGES, SAMPLE_APP, CommerceRepo, revision

pytestmark = [pytest.mark.integration, pytest.mark.docker]
S = RunState
REPOSITORY = "example-org/commerce-demo"
SAFE, F1 = "ledger-backfill-01", "ledger-backfill-07"
C1, C2, C3 = (
    "commerce.c1_event_preservation",
    "commerce.c2_refund_totals",
    "commerce.c3_capture_totals",
)
SETTINGS = WorkerSettings(
    lease=timedelta(minutes=5),
    heartbeat_interval=timedelta(seconds=10),
    review_ttl=timedelta(hours=1),
    retry=FAST_RETRY,
)


@pytest.fixture(scope="session")
def commerce_repo(tmp_path_factory: pytest.TempPathFactory) -> CommerceRepo:
    return CommerceRepo(tmp_path_factory.mktemp("commerce-repo") / "repo")


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

    def pipeline(self, project: ProjectConfig) -> Pipeline:
        return Pipeline(
            project=project,
            clone=self.repo.path,
            backend=self.backend,
            artifacts=self.artifacts,
            results=self.results,
            baseline=CommerceBaseline(),
        )

    def worker(
        self, pipeline: Pipeline, **overrides: Callable[[StageContext], StageOutcome]
    ) -> Worker:
        replaced = {RunState[name.upper()]: handler for name, handler in overrides.items()}
        handlers = {**pipeline.handlers(), **replaced}
        return Worker(self.store, handlers, pipeline.cleanup, worker_id="w1", settings=SETTINGS)

    def submit(self, project: ProjectConfig, head: str, *, pull_request: int = 7) -> UUID:
        inputs = RunInputs(
            repository=REPOSITORY,
            pull_request=pull_request,
            base_sha=self.repo.base,
            head_sha=head,
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
    assert len(harness.backend.resources(run_id)) == 6, "previews stay up during review"

    make_due(harness.dsn, run_id)
    assert worker.run_once()
    assert harness.states(run_id)[-3:] == [S.READY_FOR_REVIEW, S.CLEANING, S.COMPLETE]
    assert harness.states(run_id).count(S.VERIFYING) == 1, "a retry repeats the stage in place"
    assert harness.backend.resources(run_id) == []
    assert harness.store.transitions(run_id)[-1].detail["cleanup"] == "SUCCEEDED"


def test_f1_is_blocked_with_the_refund_evidence_and_reproduces_exactly(harness: Harness) -> None:
    head = harness.repo.change(F1)
    project = harness.project(head)
    worker = harness.worker(harness.pipeline(project))
    first = harness.submit(project, head, pull_request=7)
    second = harness.submit(project, head, pull_request=8)
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
