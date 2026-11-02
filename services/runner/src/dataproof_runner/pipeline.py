"""The run pipeline: the stage handlers a worker executes for one configured project.

FETCHING      stage exact base and head archives in the artifact store
ANALYZING     refuse unsupported repositories and dependency or out-of-root changes
PROVISIONING  create reference and candidate environments, migrate both to the base schema with
              the base source, load the identical baseline, prove it identical, and serve the
              reference preview from the base source
MIGRATING     run the head's migrations in the candidate, from the head source, inside a step,
              then build and serve the candidate preview from the head source
VERIFYING     the migration and preview checks, then every protected oracle against its proposed
              binding
INVESTIGATING when a required check failed or errored: the model-off diagnosis and, if a model
              is configured, a bounded investigation of the live environments; advisory only
REPORTING     seal pg_dump snapshots of both databases, verify that each restores to the same
              rows in a fresh environment, write the evidence manifest, then derive the
              verdict and its GitHub conclusion with the coverage table

Every stage records its results through the transition's ``effects``, so results exist exactly
when the run has moved past the stage that produced them. Problems caused by the pull request
(a failing migration, a broken or unapproved binding) become check outcomes. Problems with the
trusted setup become a run ``ERROR`` with a typed code, and transient failures are retried.
"""

import hashlib
import io
import json
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

import psycopg
from pydantic import ValidationError

from dataproof_contracts import (
    CheckOutcome,
    CheckResult,
    Environment,
    ErrorCode,
    EvidenceManifest,
    EvidenceRecord,
    FailureKey,
    NotExecutedReason,
    NotRunReason,
    Oracle,
    ReportVerdict,
    Reproduction,
    ReproductionStatus,
    RestoreVerification,
    ReviewRun,
    RunState,
    SnapshotArtifact,
    SourceArtifact,
    SubsetTable,
    canonical_json,
    github_check_conclusion,
)
from dataproof_control import Effects
from dataproof_control.results import (
    CheckRecord,
    ManifestRecord,
    PreviewRecord,
    ReportRecord,
    RunResults,
    SnapshotRecord,
    SourceRecord,
    StepRecord,
    record_check,
    record_evidence,
    record_investigation,
    record_manifest,
    record_preview,
    record_report,
    record_snapshot,
    record_source,
    record_step,
)
from dataproof_investigator import Investigator, ToolContext, diagnose
from dataproof_runner.artifacts import ArtifactIntegrityError, ArtifactRef, LocalArtifactStore
from dataproof_runner.backend import (
    FRONTEND_BUILD_LIMITS,
    EnvironmentHandle,
    ExecutionBackend,
    PreviewResult,
    StagedSource,
    StepResult,
)
from dataproof_runner.baselines import Baseline, database_digest, user_tables
from dataproof_runner.preview_client import PreviewApi
from dataproof_runner.project import ProjectConfig
from dataproof_runner.source import archive_commit, changed_paths
from dataproof_runner.subset import Catalog, closure, export, load
from dataproof_runner.worker import StageContext, StageHandler, StageOutcome
from dataproof_verifier import (
    OracleFile,
    QueryLimits,
    assemble_contract,
    derive_verdict,
    parse_binding,
    parse_oracle,
    verify_api_contract,
    verify_contract,
)

MIGRATION_CHECK = "migration.execute"
MIGRATION_CONTRACT = "dataproof.migration_executes"
MIGRATION_STEP = "candidate.migration"
PREVIEW_CHECK = "preview.candidate"
PREVIEW_CONTRACT = "dataproof.preview_serves"
RESTORE_VARIANT = "restore"
REPLAY_VARIANT = "replay"
REPRODUCTION_VARIANT = "repro"
SUBSET_SOURCE_VARIANT = "reprosrc"
SEED_KEYS = 3
"""How many failing keys a reproduction starts from: the first ones, in a fixed order."""


@dataclass(frozen=True)
class ReplayReport:
    """What a replay from sealed snapshots reproduced, skipped and could not reproduce."""

    run_id: UUID
    replayed: tuple[str, ...]
    """Data contracts re-run and compared with the recorded results."""
    skipped: tuple[str, ...]
    """Checks a replay cannot reproduce from snapshots: API behaviour, and checks that did
    not run."""
    differences: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.differences


def _signature(result: CheckResult) -> list[FailureKey]:
    """The first ``SEED_KEYS`` failing keys, in a fixed order, with their differing columns."""
    columns: dict[str, set[str]] = {}
    keys: dict[str, Any] = {}
    for mismatch in result.mismatch_examples:
        encoded = json.dumps([k.model_dump(mode="json") for k in mismatch.key], sort_keys=True)
        keys[encoded] = mismatch.key
        columns.setdefault(encoded, set())
        if (
            mismatch.column is not None
            and mismatch.expected is not None
            and mismatch.observed is not None
        ):
            columns[encoded].add(mismatch.column)
    return [
        FailureKey(key=keys[encoded], columns=tuple(sorted(columns[encoded])))
        for encoded in sorted(keys)[:SEED_KEYS]
    ]


def _unconfirmed(signature: Sequence[FailureKey], result: CheckResult) -> list[FailureKey]:
    """Seed keys the result does not show failing on the same columns."""
    seen: dict[str, set[str]] = {}
    for mismatch in result.mismatch_examples:
        encoded = json.dumps([k.model_dump(mode="json") for k in mismatch.key], sort_keys=True)
        seen.setdefault(encoded, set())
        if (
            mismatch.column is not None
            and mismatch.expected is not None
            and mismatch.observed is not None
        ):
            seen[encoded].add(mismatch.column)
    return [
        key
        for key in signature
        if seen.get(json.dumps([k.model_dump(mode="json") for k in key.key], sort_keys=True))
        != set(key.columns)
    ]


def _fingerprint(result: CheckResult) -> tuple[object, ...]:
    return (
        result.outcome,
        result.error_code,
        result.not_run_reason,
        result.mismatch_count,
        result.mismatch_examples,
        result.expected_key_count,
        result.observed_key_count,
    )


def _compare_replay(
    run_id: UUID,
    results: RunResults,
    replayed: list[CheckRecord],
    evidence: Mapping[str, Mapping[str, Any]],
) -> ReplayReport:
    recorded = {c.result.check_id: c.result for c in results.checks(run_id)}
    recorded_evidence = results.evidence(run_id)
    compared: list[str] = []
    skipped: list[str] = []
    differences: list[str] = []
    for record in replayed:
        check_id = record.result.check_id
        original = recorded.get(check_id)
        if original is None:
            differences.append(f"{check_id}: not recorded by the run")
        elif (
            original.outcome is CheckOutcome.NOT_RUN
            or record.result.outcome is CheckOutcome.NOT_RUN
        ):
            skipped.append(check_id)
        elif _fingerprint(original) != _fingerprint(record.result):
            differences.append(
                f"{check_id}: {original.outcome} recorded, {record.result.outcome} replayed"
            )
        else:
            compared.append(check_id)
    for evidence_id, item in evidence.items():
        before = recorded_evidence.get(evidence_id, {}).get("result_sha256")
        if before != item.get("result_sha256"):
            differences.append(f"{evidence_id}: result digest differs")
    skipped += [c for c in recorded if c not in {r.result.check_id for r in replayed}]
    return ReplayReport(
        run_id=run_id,
        replayed=tuple(sorted(compared)),
        skipped=tuple(sorted(skipped)),
        differences=tuple(differences),
    )


EXIT_SOURCE_DIGEST_MISMATCH = 65
_MAX_CONTRACT_FILE = 64 * 1024


@dataclass(frozen=True)
class PipelineSettings:
    environment_ttl: timedelta = timedelta(hours=4)
    """Expiry label on environments: the reaper's safety net if cleanup never runs."""
    evidence_retention: timedelta = timedelta(days=30)
    query_limits: QueryLimits = field(default_factory=QueryLimits)
    previews: bool = True
    """Build and serve both environments' applications for review (work package 5)."""
    preview_timeout: timedelta = timedelta(minutes=10)


def _toml_members(archive: bytes, directory: str) -> dict[str, str]:
    """``*.toml`` files directly inside ``directory`` of a source archive, by path."""
    found: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if (
                member.isfile()
                and path.suffix == ".toml"
                and f"{path.parent}/" == directory
                and member.size <= _MAX_CONTRACT_FILE
            ):
                extracted = tar.extractfile(member)
                if extracted is not None:
                    found[member.name] = extracted.read().decode("utf-8", errors="replace")
    return dict(sorted(found.items()))


def _step_record(
    step_id: str, role: Environment, source: StagedSource, result: StepResult
) -> StepRecord:
    return StepRecord(
        step_id=step_id,
        environment=role,
        kind="migrate",
        source_sha256=source.sha256,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=int(result.duration_seconds * 1000),
        output=result.output,
    )


def _json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest_of(image: str) -> str:
    """``sha256:...`` of an image ID or of a ``name@sha256:...`` reference."""
    return image.rsplit("@", 1)[-1]


def _all(*effects: Effects) -> Effects:
    def apply(conn: Any) -> None:
        for effect in effects:
            effect(conn)

    return apply


class Pipeline:
    def __init__(
        self,
        *,
        project: ProjectConfig,
        clone: Path,
        backend: ExecutionBackend,
        artifacts: LocalArtifactStore,
        results: RunResults,
        baseline: Baseline,
        settings: PipelineSettings | None = None,
        investigator: Investigator | None = None,
    ) -> None:
        if baseline.name != project.baseline:
            raise ValueError(f"project expects baseline {project.baseline}, got {baseline.name}")
        self.project = project
        self.clone = clone
        self.backend = backend
        self.artifacts = artifacts
        self.results = results
        self.baseline = baseline
        self.settings = settings or PipelineSettings()
        self.investigator = investigator
        """Optional: without a model, failures are explained by the model-off diagnosis only."""

    def handlers(self) -> dict[RunState, StageHandler]:
        return {
            RunState.FETCHING: self.fetch,
            RunState.ANALYZING: self.analyze,
            RunState.PROVISIONING: self.provision,
            RunState.MIGRATING: self.migrate,
            RunState.VERIFYING: self.verify,
            RunState.INVESTIGATING: self.investigate,
            RunState.REPORTING: self.report,
        }

    @property
    def _migrate_arguments(self) -> list[str]:
        return ["migrate", "--config", self.project.alembic_config, "--target", "head"]

    @property
    def _step_timeout(self) -> timedelta:
        return timedelta(seconds=self.project.step_timeout_seconds)

    # FETCHING ----------------------------------------------------------------------------------

    def fetch(self, context: StageContext) -> StageOutcome:
        run = context.run
        actual = self.project.digest()
        if actual != run.inputs.config_digest:
            return StageOutcome(
                RunState.ERROR,
                error_code=ErrorCode.CONFIG_DIGEST_MISMATCH,
                detail={"pinned": run.inputs.config_digest, "configured": actual},
            )
        records = []
        for role, sha in (
            (Environment.REFERENCE, run.inputs.base_sha),
            (Environment.CANDIDATE, run.inputs.head_sha),
        ):
            staged = archive_commit(self.clone, sha)
            ref = self.artifacts.put(run.run_id, f"source-{role.value.lower()}.tar", staged.data)
            records.append(SourceRecord(role, sha, ref.sha256, ref.size_bytes, ref.object_key))
        return StageOutcome(
            RunState.ANALYZING,
            detail={
                r.environment.value: {
                    "commit": r.commit_sha,
                    "sha256": r.sha256,
                    "bytes": r.size_bytes,
                }
                for r in records
            },
            effects=_all(*(self._recorder(record_source, run.run_id, r) for r in records)),
        )

    # ANALYZING ---------------------------------------------------------------------------------

    def analyze(self, context: StageContext) -> StageOutcome:
        run = context.run
        if run.inputs.repository != self.project.repository:
            return self._not_executed(
                run,
                NotExecutedReason.REPOSITORY_NOT_ALLOWLISTED,
                {"repository": run.inputs.repository},
            )
        paths = changed_paths(self.clone, run.inputs.base_sha, run.inputs.head_sha)
        violations = self.project.policy_violations(paths)
        if violations:
            return self._not_executed(
                run,
                NotExecutedReason.DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL,
                {"paths": violations},
            )
        return StageOutcome(RunState.PROVISIONING, detail={"changed_paths": paths})

    def _not_executed(
        self, run: ReviewRun, reason: NotExecutedReason, detail: Mapping[str, Any]
    ) -> StageOutcome:
        report = ReportRecord(
            verdict=ReportVerdict.NOT_EXECUTED,
            conclusion=github_check_conclusion(ReportVerdict.NOT_EXECUTED),
            report={
                **self._provenance(run),
                "not_executed_reason": reason.value,
                **detail,
                "coverage": [],
            },
        )
        return StageOutcome(
            RunState.NOT_EXECUTED,
            not_executed_reason=reason,
            detail=detail,
            effects=self._recorder(record_report, run.run_id, report),
        )

    # PROVISIONING ------------------------------------------------------------------------------

    def provision(self, context: StageContext) -> StageOutcome:
        run = context.run
        if self.baseline.digest() != run.inputs.dataset_digest:
            return StageOutcome(
                RunState.ERROR,
                error_code=ErrorCode.DATASET_DIGEST_MISMATCH,
                detail={"pinned": run.inputs.dataset_digest, "baseline": self.baseline.digest()},
            )
        self.backend.destroy(run.run_id)  # a retried attempt starts from nothing
        base = self._source(run.run_id, Environment.REFERENCE)
        expires = datetime.now(UTC) + self.settings.environment_ttl
        steps: list[StepRecord] = []
        digests: dict[str, str] = {}
        isolation: list[str] = []
        handles: dict[Environment, EnvironmentHandle] = {}
        for role in (Environment.REFERENCE, Environment.CANDIDATE):
            context.ensure_lease()
            handle, step, error = self._baseline_environment(run.run_id, role, base, expires)
            steps.append(step)
            if error is not None:
                return StageOutcome(
                    RunState.ERROR,
                    error_code=error,
                    detail={
                        "step": step.step_id,
                        "exit_code": step.exit_code,
                        "timed_out": step.timed_out,
                    },
                    effects=self._steps(run.run_id, steps),
                )
            if handle is None:
                raise RuntimeError(f"no {role.value.lower()} environment was provisioned")
            handles[role] = handle
            isolation.append(handle.isolation_id)
            with psycopg.connect(handle.database.conninfo(handle.database.reader)) as conn:
                digests[role.value] = database_digest(conn, self.baseline.tables)
        if len(set(digests.values())) != 1:
            return StageOutcome(
                RunState.ERROR,
                error_code=ErrorCode.BASELINE_DIVERGED,
                detail={"database_digests": digests},
                effects=self._steps(run.run_id, steps),
            )
        effects = self._steps(run.run_id, steps)
        if self.settings.previews:
            context.ensure_lease()
            preview = self._preview(handles[Environment.REFERENCE], base)
            effects = _all(effects, self._recorder(record_preview, run.run_id, preview))
            if not preview.ready:
                return StageOutcome(
                    RunState.ERROR,
                    error_code=ErrorCode.REFERENCE_PREVIEW_FAILED,
                    detail={"failure": preview.failure},
                    effects=effects,
                )
        return StageOutcome(
            RunState.MIGRATING,
            isolation_id=isolation[0].rsplit("-", 1)[0],  # the run's prefix, without the role
            detail={
                "backend": self.backend.name,
                "environments": isolation,
                "dataset_digest": run.inputs.dataset_digest,
                "database_digest": digests[Environment.REFERENCE.value],
            },
            effects=effects,
        )

    def _baseline_environment(
        self, run_id: UUID, role: Environment, base: StagedSource, expires: datetime
    ) -> tuple[EnvironmentHandle | None, StepRecord, ErrorCode | None]:
        """Provision one role, migrate it to the base schema and load the baseline."""
        handle = self.backend.provision(run_id, role, expires_at=expires)
        result = self.backend.run_step(
            handle,
            image=self.project.runtime_image,
            source=base,
            arguments=self._migrate_arguments,
            timeout=self._step_timeout,
        )
        step = _step_record(f"{role.value.lower()}.base-migration", role, base, result)
        if result.exit_code == EXIT_SOURCE_DIGEST_MISMATCH:
            return None, step, ErrorCode.SOURCE_DIGEST_MISMATCH
        if not result.succeeded:
            return None, step, ErrorCode.BASE_MIGRATION_FAILED
        with psycopg.connect(handle.database.conninfo(handle.database.owner)) as conn:
            self.baseline.load(conn)
        return handle, step, None

    # MIGRATING ---------------------------------------------------------------------------------

    def migrate(self, context: StageContext) -> StageOutcome:
        run = context.run
        candidate = self.backend.attach(run.run_id, Environment.CANDIDATE)
        if candidate is None or context.claim.attempt > 1:
            # A previous attempt may have left the candidate partly migrated: rebuild it so that
            # the recorded migration is one clean execution from the identical baseline.
            self.backend.destroy(run.run_id, role=Environment.CANDIDATE)
            base = self._source(run.run_id, Environment.REFERENCE)
            expires = datetime.now(UTC) + self.settings.environment_ttl
            candidate, step, error = self._baseline_environment(
                run.run_id, Environment.CANDIDATE, base, expires
            )
            if error is not None or candidate is None:
                raise RuntimeError(
                    f"could not rebuild the candidate baseline: {step.output[-500:]}"
                )
        context.ensure_lease()
        head = self._source(run.run_id, Environment.CANDIDATE)
        result = self.backend.run_step(
            candidate,
            image=self.project.runtime_image,
            source=head,
            arguments=self._migrate_arguments,
            timeout=self._step_timeout,
        )
        step = _step_record(MIGRATION_STEP, Environment.CANDIDATE, head, result)
        if result.exit_code == EXIT_SOURCE_DIGEST_MISMATCH:
            return StageOutcome(
                RunState.ERROR,
                error_code=ErrorCode.SOURCE_DIGEST_MISMATCH,
                effects=self._steps(run.run_id, [step]),
            )
        effects = self._steps(run.run_id, [step])
        detail: dict[str, Any] = {
            "succeeded": result.succeeded,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_ms": step.duration_ms,
        }
        if result.succeeded and self.settings.previews:
            context.ensure_lease()
            preview = self._preview(candidate, head)
            effects = _all(effects, self._recorder(record_preview, run.run_id, preview))
            detail["preview"] = {"ready": preview.ready, "failure": preview.failure}
        return StageOutcome(RunState.VERIFYING, detail=detail, effects=effects)

    def _preview(self, environment: EnvironmentHandle, source: StagedSource) -> PreviewRecord:
        """Build and serve one environment's application from exactly this source."""
        result: PreviewResult = self.backend.start_preview(
            environment,
            image=self.project.runtime_image,
            source=source,
            arguments=[
                "serve",
                "--app-dir",
                self.project.app_dir,
                "--web-dir",
                self.project.web_dir,
            ],
            timeout=self.settings.preview_timeout,
            limits=FRONTEND_BUILD_LIMITS,
        )
        access = result.access
        return PreviewRecord(
            environment=environment.role,
            commit_sha=result.commit_sha,
            ready=result.ready,
            failure=result.failure,
            url=access.url if access else None,
            token_sha256=hashlib.sha256(access.token.encode()).hexdigest() if access else None,
            ready_seconds=result.ready_seconds,
            build_seconds=result.build_seconds,
            peak_memory_bytes=result.peak_memory_bytes,
            cpu_seconds=result.cpu_seconds,
            output=result.output,
        )

    # VERIFYING ---------------------------------------------------------------------------------

    def verify(self, context: StageContext) -> StageOutcome:
        run = context.run
        dataset = run.inputs.dataset_digest
        (migration,) = [s for s in self.results.steps(run.run_id) if s.step_id == MIGRATION_STEP]
        migration_evidence = f"{MIGRATION_CHECK}:CANDIDATE:step"
        checks: list[CheckRecord] = [
            CheckRecord(
                CheckResult(
                    check_id=MIGRATION_CHECK,
                    contract_id=MIGRATION_CONTRACT,
                    contract_version=1,
                    required=True,
                    outcome=CheckOutcome.PASS if migration.succeeded else CheckOutcome.FAIL,
                    evidence_ids=(migration_evidence,),
                    dataset_digest=dataset,
                    elapsed_ms=migration.duration_ms,
                )
            )
        ]
        evidence: dict[str, Mapping[str, Any]] = {
            migration_evidence: {
                "kind": "step",
                "step_id": migration.step_id,
                "exit_code": migration.exit_code,
                "timed_out": migration.timed_out,
                "source_sha256": migration.source_sha256,
            }
        }
        if self.settings.previews:
            checks.append(self._preview_check(run, migration.succeeded, evidence))
        if migration.succeeded:
            checks += self._contract_checks(run, evidence)
        else:
            checks += [
                CheckRecord(self._not_run(spec, NotRunReason.PREREQUISITE_FAILED, dataset))
                for spec in self._oracle_specs(run)
            ]
        summary = {c.result.check_id: c.result.outcome.value for c in checks}
        failed = any(
            c.result.required and c.result.outcome in (CheckOutcome.FAIL, CheckOutcome.ERROR)
            for c in checks
        )
        return StageOutcome(
            RunState.INVESTIGATING if failed else RunState.REPORTING,
            detail={"outcomes": summary},
            effects=_all(
                *(self._recorder(record_check, run.run_id, check) for check in checks),
                *(
                    self._recorder(record_evidence, run.run_id, evidence_id, record)
                    for evidence_id, record in sorted(evidence.items())
                ),
            ),
        )

    def _preview_check(
        self, run: ReviewRun, migrated: bool, evidence: dict[str, Mapping[str, Any]]
    ) -> CheckRecord:
        """Required: the candidate application builds from its source and serves (ADR-0004)."""
        common: dict[str, Any] = {
            "check_id": PREVIEW_CHECK,
            "contract_id": PREVIEW_CONTRACT,
            "contract_version": 1,
            "required": True,
            "dataset_digest": run.inputs.dataset_digest,
        }
        preview = self.results.previews(run.run_id).get(Environment.CANDIDATE)
        if not migrated or preview is None:
            return CheckRecord(
                CheckResult(
                    outcome=CheckOutcome.NOT_RUN,
                    not_run_reason=NotRunReason.PREREQUISITE_FAILED,
                    **common,
                )
            )
        evidence_id = f"{PREVIEW_CHECK}:CANDIDATE:preview"
        evidence[evidence_id] = {
            "kind": "preview",
            "commit_sha": preview.commit_sha,
            "ready": preview.ready,
            "failure": preview.failure,
            "build_seconds": preview.build_seconds,
            "ready_seconds": preview.ready_seconds,
            "peak_memory_bytes": preview.peak_memory_bytes,
            "cpu_seconds": preview.cpu_seconds,
        }
        if preview.ready:
            outcome, error = CheckOutcome.PASS, None
        else:
            outcome = CheckOutcome.ERROR
            error = (
                ErrorCode.CANDIDATE_BUILD_FAILED
                if preview.failure == "build"
                else ErrorCode.CANDIDATE_START_FAILED
            )
        return CheckRecord(
            CheckResult(
                outcome=outcome,
                error_code=error,
                evidence_ids=(evidence_id,),
                elapsed_ms=int(preview.ready_seconds * 1000),
                **common,
            )
        )

    def _oracle_specs(self, run: ReviewRun) -> list[tuple[OracleFile, Oracle]]:
        base = self._source(run.run_id, Environment.REFERENCE)
        return [
            parse_oracle(text, source_path=path, base_sha=run.inputs.base_sha)
            for path, text in _toml_members(base.data, self.project.oracle_dir).items()
        ]

    def _not_run(
        self, oracle: tuple[OracleFile, Oracle], reason: NotRunReason, dataset: str
    ) -> CheckResult:
        spec, _ = oracle
        return CheckResult(
            check_id=spec.contract_id,
            contract_id=spec.contract_id,
            contract_version=spec.version,
            required=spec.required,
            outcome=CheckOutcome.NOT_RUN,
            not_run_reason=reason,
            dataset_digest=dataset,
        )

    def _contract_checks(
        self,
        run: ReviewRun,
        evidence: dict[str, Mapping[str, Any]],
        environments: tuple[EnvironmentHandle, EnvironmentHandle] | None = None,
        apis: tuple[PreviewApi, PreviewApi] | None = None,
        *,
        live: bool = True,
    ) -> list[CheckRecord]:
        """Every oracle against its proposed binding.

        ``environments`` defaults to the run's own; ``live=False`` (a replay) evaluates data
        contracts only and records API contracts as not run, because they need live previews.
        """
        dataset = run.inputs.dataset_digest
        head = self._source(run.run_id, Environment.CANDIDATE)
        proposed: dict[str, list[tuple[str, Any]]] = {}
        problems: dict[str, str] = {}
        for path, text in _toml_members(head.data, self.project.binding_dir).items():
            try:
                contract_id, binding = parse_binding(
                    text, source_path=path, head_sha=run.inputs.head_sha
                )
            except (ValueError, ValidationError) as error:
                problems[path] = str(error).splitlines()[0]
                continue
            proposed.setdefault(contract_id, []).append((path, binding))

        reference, candidate = environments or (
            self._attached(run.run_id, Environment.REFERENCE),
            self._attached(run.run_id, Environment.CANDIDATE),
        )
        if live and apis is None:
            apis = self._preview_apis(run.run_id)
        records: list[CheckRecord] = []
        with (
            psycopg.connect(
                reference.database.conninfo(reference.database.reader), autocommit=True
            ) as ref,
            psycopg.connect(
                candidate.database.conninfo(candidate.database.reader), autocommit=True
            ) as cand,
        ):
            for oracle in self._oracle_specs(run):
                spec, oracle_model = oracle
                bindings = proposed.get(spec.contract_id, [])
                if len(bindings) != 1:
                    problem = (
                        f"{len(bindings)} binding files propose {spec.contract_id}"
                        if bindings
                        else None
                    )
                    result = self._not_run(oracle, NotRunReason.AWAITING_BINDING_REVIEW, dataset)
                    records.append(CheckRecord(result, binding_problem=problem))
                    continue
                (_, binding) = bindings[0]
                contract = assemble_contract(
                    spec,
                    oracle_model,
                    binding,
                    approved_digests=self.project.approved_binding_digests,
                    rejected_digests=self.project.rejected_binding_digests,
                )
                if oracle_model.api_route is not None and apis is None:
                    # API behaviour needs both previews serving; without them it did not run.
                    result = self._not_run(oracle, NotRunReason.PREREQUISITE_FAILED, dataset)
                    records.append(CheckRecord(result))
                    continue
                if oracle_model.api_route is not None and apis is not None:
                    verification = verify_api_contract(
                        contract, ref, apis[0], apis[1], dataset, self.settings.query_limits
                    )
                else:
                    verification = verify_contract(
                        contract, ref, cand, dataset, self.settings.query_limits
                    )
                for item in verification.evidence:
                    evidence[item.evidence_id] = {
                        "kind": "api" if item.evidence_id.endswith(":api") else "query",
                        "environment": item.environment.value,
                        "query_sql": item.query_sql,
                        "sql_sha256": item.sql_sha256,
                        "result_sha256": item.result_sha256,
                        "row_count": item.row_count,
                    }
                records.append(
                    CheckRecord(
                        verification.check,
                        binding_analysis=verification.binding_analysis,
                        binding_problem=verification.binding_problem,
                    )
                )
        if problems:
            # Unparseable binding files are reported on the report, not silently dropped.
            evidence["bindings:CANDIDATE:invalid"] = {"kind": "invalid_bindings", "files": problems}
        return records

    def _preview_apis(self, run_id: UUID) -> tuple[PreviewApi, PreviewApi] | None:
        """API clients for both previews, or None unless both are serving."""
        if not self.settings.previews:
            return None
        clients = []
        for role in (Environment.REFERENCE, Environment.CANDIDATE):
            handle = self.backend.attach(run_id, role)
            access = self.backend.preview_access(handle) if handle is not None else None
            if access is None:
                return None
            clients.append(PreviewApi(access))
        return clients[0], clients[1]

    # INVESTIGATING -----------------------------------------------------------------------------

    def investigate(self, context: StageContext) -> StageOutcome:
        """Explain the failures; advisory only (ADR-0002).

        The model-off diagnosis always runs. A configured model investigates the live
        environments through the read-only role and the head commit's allowlisted source. Both
        results are recorded beside the checks; neither can change them, and a model that fails
        or declines leaves the deterministic report as it is.
        """
        run = context.run
        checks = {c.result.check_id: c.result for c in self.results.checks(run.run_id)}
        investigations = [diagnose(run.run_id, checks)]
        if self.investigator is not None:
            context.ensure_lease()
            reference = self._attached(run.run_id, Environment.REFERENCE)
            candidate = self._attached(run.run_id, Environment.CANDIDATE)
            tools = ToolContext(
                checks=checks,
                evidence=self.results.evidence(run.run_id),
                source_archive=self._source(run.run_id, Environment.CANDIDATE).data,
                source_roots=self.project.investigator_source_roots,
                databases={
                    handle.role: handle.database.conninfo(handle.database.reader)
                    for handle in (reference, candidate)
                },
            )
            investigations.append(self.investigator.investigate(run.run_id, tools))
        return StageOutcome(
            RunState.REPORTING,
            detail={
                "investigations": [
                    {"model": i.model, "status": i.status.value} for i in investigations
                ]
            },
            effects=_all(*(self._recorder(record_investigation, i) for i in investigations)),
        )

    # Replay ------------------------------------------------------------------------------------

    def replay(self, run: ReviewRun) -> "ReplayReport":
        """Re-run a sealed run's data contracts from its snapshots alone, and compare.

        Nothing of the run's own environments is used: the sealed manifest and snapshots are
        re-verified by SHA-256, restored into fresh ``replay`` environments, checked exactly as
        ``VERIFYING`` checks, and the environments are destroyed again. A replay never changes
        the run or its results.
        """
        if self.project.digest() != run.inputs.config_digest:
            raise ValueError("this run was pinned to another configuration; it cannot be replayed")
        sealed = self.results.manifest(run.run_id)
        if sealed is None:
            raise ValueError(f"run {run.run_id} has no sealed evidence")
        if self.artifacts.get(sealed.object_key, sealed.sha256) != canonical_json(sealed.manifest):
            raise ArtifactIntegrityError("the stored manifest does not match its record")
        snapshots = {s.environment: s for s in sealed.manifest.snapshots}
        handles: list[EnvironmentHandle] = []
        try:
            for role in (Environment.REFERENCE, Environment.CANDIDATE):
                dump = self.artifacts.get(snapshots[role].object_key, snapshots[role].sha256)
                self.backend.destroy(run.run_id, role=role, variant=REPLAY_VARIANT)
                handle = self.backend.provision(
                    run.run_id,
                    role,
                    expires_at=datetime.now(UTC) + self.settings.environment_ttl,
                    variant=REPLAY_VARIANT,
                )
                self.backend.restore_database(handle, dump)
                handles.append(handle)
            evidence: dict[str, Mapping[str, Any]] = {}
            replayed = self._contract_checks(run, evidence, (handles[0], handles[1]), live=False)
        finally:
            for role in (Environment.REFERENCE, Environment.CANDIDATE):
                self.backend.destroy(run.run_id, role=role, variant=REPLAY_VARIANT)
        return _compare_replay(run.run_id, self.results, replayed, evidence)

    # Reproduction ------------------------------------------------------------------------------

    def reproduce(self, run: ReviewRun, check_id: str) -> Reproduction:
        """Replay one failed check on an FK-closure subset of the sealed baseline.

        The subset follows a fixed rule (``subset``), starting from the check's first
        ``SEED_KEYS`` failing keys. It is loaded into fresh reference and candidate
        environments; the candidate runs the head migration from the head source; the contract
        is evaluated again. ``REPRODUCED`` needs every seed key to fail again on the same
        columns. Anything else is ``NOT_REPRODUCED`` with the reason; the subset is never
        widened to force a result. Every environment used is destroyed again, and the run
        itself is never changed.
        """
        recorded = {c.result.check_id: c.result for c in self.results.checks(run.run_id)}
        original = recorded.get(check_id)
        if original is None or original.outcome is not CheckOutcome.FAIL:
            raise ValueError(f"{check_id} did not fail in run {run.run_id}")
        if self.project.digest() != run.inputs.config_digest:
            raise ValueError("this run was pinned to another configuration")
        sealed = self.results.manifest(run.run_id)
        if sealed is None:
            raise ValueError(f"run {run.run_id} has no sealed evidence")
        oracle = next((o for o in self._oracle_specs(run) if o[0].contract_id == check_id), None)
        if oracle is None or oracle[1].api_route is not None:
            raise ValueError(f"{check_id} is not a data contract of this run")
        key_columns = oracle[1].key_columns
        signature = _signature(original)
        seeds = [tuple(getattr(k, "value", None) for k in fk.key) for fk in signature]

        def outcome(
            status: ReproductionStatus,
            *,
            reason: str | None = None,
            table: str | None = None,
            data: Mapping[str, str] | None = None,
            stored: ArtifactRef | None = None,
            result: CheckResult | None = None,
        ) -> Reproduction:
            reproduction = Reproduction(
                reproduction_id=uuid4(),
                run_id=run.run_id,
                check_id=check_id,
                status=status,
                signature=tuple(signature),
                seed_table=table,
                subset=tuple(
                    SubsetTable(table=t, rows=text.count("\n"))
                    for t, text in sorted((data or {}).items())
                ),
                subset_sha256=stored.sha256 if stored else None,
                object_key=stored.object_key if stored else None,
                result=result,
                reason=reason,
                created_at=datetime.now(UTC),
            )
            self.results.save_reproduction(reproduction)
            return reproduction

        snapshot = next(
            s for s in sealed.manifest.snapshots if s.environment is Environment.REFERENCE
        )
        dump = self.artifacts.get(snapshot.object_key, snapshot.sha256)
        try:
            source = self._fresh(run.run_id, Environment.REFERENCE, SUBSET_SOURCE_VARIANT)
            self.backend.restore_database(source, dump)
            with psycopg.connect(source.database.conninfo(source.database.owner)) as conn:
                catalog = Catalog.read(conn)
                table = catalog.seed_table(key_columns)
                if table is None:
                    return outcome(
                        ReproductionStatus.NOT_REPRODUCED,
                        reason=f"no baseline table holds the keys {list(key_columns)}",
                    )
                data = export(conn, catalog, closure(conn, catalog, table, key_columns, seeds))
        finally:
            self.backend.destroy(run.run_id, variant=SUBSET_SOURCE_VARIANT)
        stored = self.artifacts.put(
            run.run_id, f"reproduction-{check_id}.json", _json({"tables": dict(data)})
        )

        try:
            environments: list[EnvironmentHandle] = []
            for role in (Environment.REFERENCE, Environment.CANDIDATE):
                handle = self._fresh(run.run_id, role, REPRODUCTION_VARIANT)
                base = self.backend.run_step(
                    handle,
                    image=self.project.runtime_image,
                    source=self._source(run.run_id, Environment.REFERENCE),
                    arguments=self._migrate_arguments,
                    timeout=self._step_timeout,
                )
                if not base.succeeded:
                    raise RuntimeError("the base migration failed while preparing a reproduction")
                with psycopg.connect(handle.database.conninfo(handle.database.owner)) as conn:
                    load(conn, data)
                environments.append(handle)
            migrated = self.backend.run_step(
                environments[1],
                image=self.project.runtime_image,
                source=self._source(run.run_id, Environment.CANDIDATE),
                arguments=self._migrate_arguments,
                timeout=self._step_timeout,
            )
            if not migrated.succeeded:
                return outcome(
                    ReproductionStatus.NOT_REPRODUCED,
                    reason="the candidate migration did not run on the subset",
                    table=table,
                    data=data,
                    stored=stored,
                )
            evidence: dict[str, Mapping[str, Any]] = {}
            checks = self._contract_checks(
                run, evidence, (environments[0], environments[1]), live=False
            )
            (result,) = [c.result for c in checks if c.result.check_id == check_id]
        finally:
            for role in (Environment.REFERENCE, Environment.CANDIDATE):
                self.backend.destroy(run.run_id, role=role, variant=REPRODUCTION_VARIANT)

        missing = _unconfirmed(signature, result)
        if result.outcome is CheckOutcome.FAIL and not missing:
            return outcome(
                ReproductionStatus.REPRODUCED,
                table=table,
                data=data,
                stored=stored,
                result=result,
            )
        reason = (
            f"the check is {result.outcome.value} on the subset"
            if result.outcome is not CheckOutcome.FAIL
            else f"{len(missing)} seed key(s) did not fail the same way on the subset"
        )
        return outcome(
            ReproductionStatus.NOT_REPRODUCED,
            reason=reason,
            table=table,
            data=data,
            stored=stored,
            result=result,
        )

    def _fresh(self, run_id: UUID, role: Environment, variant: str) -> EnvironmentHandle:
        self.backend.destroy(run_id, role=role, variant=variant)
        return self.backend.provision(
            run_id,
            role,
            expires_at=datetime.now(UTC) + self.settings.environment_ttl,
            variant=variant,
        )

    # REPORTING ---------------------------------------------------------------------------------

    def report(self, context: StageContext) -> StageOutcome:
        run = context.run
        checks = self.results.checks(run.run_id)
        snapshots = [self._seal(context, role) for role in Environment]
        manifest = self._manifest(run, checks, snapshots)
        results = [record.result for record in checks]
        verdict = derive_verdict(results)
        report = ReportRecord(
            verdict=verdict,
            conclusion=github_check_conclusion(verdict),
            report={
                **self._provenance(run),
                "coverage": [
                    {
                        "check_id": c.check_id,
                        "required": c.required,
                        "outcome": c.outcome.value,
                        "reason": reason.value
                        if (reason := c.not_run_reason or c.error_code)
                        else None,
                        "mismatch_count": c.mismatch_count,
                    }
                    for c in results
                ],
                "investigations": [
                    {
                        "advisory": True,
                        "model": i.model,
                        "status": i.status.value,
                        "hypotheses": [
                            {
                                "statement": h.statement,
                                "cited_evidence_ids": list(h.cited_evidence_ids),
                                "confidence": h.confidence.value,
                            }
                            for h in i.hypotheses
                        ],
                        "suggested_action": i.suggested_action,
                        "caveat": i.caveat,
                        "failure": i.failure,
                    }
                    for i in self.results.investigations(run.run_id)
                ],
                "previews": {
                    p.environment.value: {
                        "url": p.url,
                        "commit_sha": p.commit_sha,
                        "ready": p.ready,
                        "failure": p.failure,
                        "build_seconds": p.build_seconds,
                        "ready_seconds": p.ready_seconds,
                        "peak_memory_bytes": p.peak_memory_bytes,
                        "cpu_seconds": p.cpu_seconds,
                    }
                    for p in self.results.previews(run.run_id).values()
                },
                "evidence": {
                    "manifest_sha256": manifest.sha256,
                    "snapshots": {
                        s.environment.value: {
                            "sha256": s.sha256,
                            "restore_verified": s.restore_verified,
                        }
                        for s in snapshots
                    },
                },
            },
        )
        return StageOutcome(
            RunState.READY_FOR_REVIEW,
            detail={
                "verdict": verdict.value,
                "conclusion": report.conclusion.value,
                "manifest_sha256": manifest.sha256,
                "restore_verified": all(s.restore_verified for s in snapshots),
            },
            effects=_all(
                *(self._recorder(record_snapshot, run.run_id, s) for s in snapshots),
                self._recorder(record_manifest, run.run_id, manifest),
                self._recorder(record_report, run.run_id, report),
            ),
        )

    def _seal(self, context: StageContext, role: Environment) -> SnapshotRecord:
        """Snapshot one environment, then prove the snapshot restores to the same rows.

        The restore happens in a fresh environment of the same role, which is destroyed again;
        the live environment stays up for review.
        """
        context.ensure_lease()
        run_id = context.run.run_id
        live = self._attached(run_id, role)
        dump = self.backend.dump_database(live)
        captured_at = datetime.now(UTC)
        ref = self.artifacts.put(run_id, f"snapshot-{role.value.lower()}.dump", dump)
        live_digest = self._digest(live)

        self.backend.destroy(run_id, role=role, variant=RESTORE_VARIANT)
        restored_digest: str | None = None
        error: str | None = None
        try:
            fresh = self.backend.provision(
                run_id,
                role,
                expires_at=datetime.now(UTC) + self.settings.environment_ttl,
                variant=RESTORE_VARIANT,
            )
            self.backend.restore_database(fresh, self.artifacts.get(ref.object_key, ref.sha256))
            restored_digest = self._digest(fresh)
        except RuntimeError as failure:
            error = str(failure)[:500]
        finally:
            self.backend.destroy(run_id, role=role, variant=RESTORE_VARIANT)
        return SnapshotRecord(
            environment=role,
            object_key=ref.object_key,
            sha256=ref.sha256,
            size_bytes=ref.size_bytes,
            captured_at=captured_at,
            database_digest=live_digest,
            restored_digest=restored_digest,
            verified_at=datetime.now(UTC),
            restore_error=error,
        )

    @staticmethod
    def _digest(environment: EnvironmentHandle) -> str:
        access = environment.database
        with psycopg.connect(access.conninfo(access.reader)) as conn:
            return database_digest(conn, user_tables(conn))

    def _manifest(
        self, run: ReviewRun, checks: list[CheckRecord], snapshots: list[SnapshotRecord]
    ) -> ManifestRecord:
        """Seal the run's evidence: every artifact and record, each with its SHA-256."""
        run_id = run.run_id
        bundle = {
            "checks": [c.result.model_dump(mode="json") for c in checks],
            "evidence": dict(self.results.evidence(run_id)),
            "steps": [
                {
                    "step_id": s.step_id,
                    "environment": s.environment.value,
                    "source_sha256": s.source_sha256,
                    "exit_code": s.exit_code,
                    "timed_out": s.timed_out,
                    "duration_ms": s.duration_ms,
                    "output": s.output,
                }
                for s in self.results.steps(run_id)
            ],
        }
        stored = self.artifacts.put(run_id, "results.json", _json(bundle))
        sealed_at = datetime.now(UTC)
        binding_digests = sorted(
            c.binding_analysis.binding_digest for c in checks if c.binding_analysis is not None
        )
        manifest = EvidenceManifest(
            run_id=run_id,
            input_digest=run.input_digest,
            dataset_digest=run.inputs.dataset_digest,
            config_digest=run.inputs.config_digest,
            binding_digest=hashlib.sha256("\n".join(binding_digests).encode()).hexdigest()
            if binding_digests
            else None,
            image_digests=tuple(
                sorted(
                    {
                        _digest_of(self.project.runtime_image),
                        _digest_of(self.backend.database_image),
                    }
                )
            ),
            source_artifacts=tuple(
                SourceArtifact(
                    environment=s.environment,
                    commit_sha=s.commit_sha,
                    object_key=s.object_key,
                    sha256=s.sha256,
                )
                for s in self.results.sources(run_id).values()
            ),
            snapshots=tuple(
                SnapshotArtifact(
                    environment=s.environment,
                    object_key=s.object_key,
                    sha256=s.sha256,
                    size_bytes=s.size_bytes,
                    captured_at=s.captured_at,
                )
                for s in snapshots
            ),
            restore_verifications=tuple(
                RestoreVerification(
                    snapshot_sha256=s.sha256, verified_at=s.verified_at, restored=s.restore_verified
                )
                for s in snapshots
            ),
            records=(
                EvidenceRecord(
                    evidence_id="run.results",
                    kind="results_bundle",
                    object_key=stored.object_key,
                    sha256=stored.sha256,
                    created_at=sealed_at,
                ),
            ),
            sealed_at=sealed_at,
            retain_until=sealed_at + self.settings.evidence_retention,
        )
        sealed = self.artifacts.put(run_id, "manifest.json", canonical_json(manifest))
        return ManifestRecord(object_key=sealed.object_key, sha256=sealed.sha256, manifest=manifest)

    # CLEANING ----------------------------------------------------------------------------------

    def cleanup(self, context: StageContext) -> Mapping[str, Any]:
        report = self.backend.destroy(context.run.run_id)
        return {
            "cleanup": report.outcome.value,
            "backend": self.backend.name,
            "removed": list(report.removed),
        }

    # Helpers -----------------------------------------------------------------------------------

    def _provenance(self, run: ReviewRun) -> dict[str, Any]:
        return {
            "repository": run.inputs.repository,
            "pull_request": run.inputs.pull_request,
            "base_sha": run.inputs.base_sha,
            "head_sha": run.inputs.head_sha,
            "config_digest": run.inputs.config_digest,
            "dataset_digest": run.inputs.dataset_digest,
            "engine_version": run.inputs.engine_version,
            "backend": self.backend.name,
        }

    def _source(self, run_id: UUID, role: Environment) -> StagedSource:
        record = self.results.sources(run_id)[role]
        data = self.artifacts.get(record.object_key, record.sha256)
        return StagedSource(commit_sha=record.commit_sha, data=data, sha256=record.sha256)

    def _attached(self, run_id: UUID, role: Environment) -> EnvironmentHandle:
        handle = self.backend.attach(run_id, role)
        if handle is None:
            raise RuntimeError(f"the {role.value.lower()} environment of run {run_id} is gone")
        return handle

    def _steps(self, run_id: UUID, steps: list[StepRecord]) -> Effects:
        return _all(*(self._recorder(record_step, run_id, step) for step in steps))

    @staticmethod
    def _recorder(function: Callable[..., None], *arguments: Any) -> Effects:
        def effect(conn: Any) -> None:
            function(conn, *arguments)

        return effect
