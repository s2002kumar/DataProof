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
from uuid import UUID

import psycopg
import pytest

from commerce_scenario import PUBLISHED_DIGEST
from control_env import FAST_RETRY, create, make_due
from dataproof_contracts import (
    CheckOutcome,
    DecimalValue,
    Environment,
    ErrorCode,
    GitHubCheckConclusion,
    IntegerValue,
    NotExecutedReason,
    NotRunReason,
    ReportVerdict,
    RunInputs,
    RunState,
    canonical_json,
)
from dataproof_control import RunResults, RunStore
from dataproof_runner import StageContext, StageOutcome, Worker, WorkerSettings
from dataproof_runner.artifacts import ArtifactIntegrityError, LocalArtifactStore
from dataproof_runner.baselines import CommerceBaseline
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.pipeline import MIGRATION_CHECK, PREVIEW_CHECK, Pipeline, PipelineSettings
from dataproof_runner.project import ProjectConfig
from dataproof_verifier import parse_binding
from docker_env import BINDING_DIR, CHANGES, SAMPLE_APP, CommerceRepo, http_get, revision

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

    def pipeline(self, project: ProjectConfig, *, previews: bool = False) -> Pipeline:
        """Previews are off unless a test is about them: each one builds a frontend."""
        return Pipeline(
            project=project,
            clone=self.repo.path,
            backend=self.backend,
            artifacts=self.artifacts,
            results=self.results,
            baseline=CommerceBaseline(),
            settings=PipelineSettings(previews=previews),
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
