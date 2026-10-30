"""The run pipeline: the stage handlers a worker executes for one configured project.

FETCHING      stage exact base and head archives in the artifact store
ANALYZING     refuse unsupported repositories and dependency or out-of-root changes
PROVISIONING  create reference and candidate environments, migrate both to the base schema with
              the base source, load the identical baseline, and prove it identical
MIGRATING     run the head's migrations in the candidate, from the head source, inside a step
VERIFYING     the migration check, then every protected oracle against its proposed binding
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from dataproof_contracts import (
    CheckOutcome,
    CheckResult,
    Environment,
    ErrorCode,
    EvidenceManifest,
    EvidenceRecord,
    NotExecutedReason,
    NotRunReason,
    Oracle,
    ReportVerdict,
    RestoreVerification,
    ReviewRun,
    RunState,
    SnapshotArtifact,
    SourceArtifact,
    canonical_json,
    github_check_conclusion,
)
from dataproof_control import Effects
from dataproof_control.results import (
    CheckRecord,
    ManifestRecord,
    ReportRecord,
    RunResults,
    SnapshotRecord,
    SourceRecord,
    StepRecord,
    record_check,
    record_evidence,
    record_manifest,
    record_report,
    record_snapshot,
    record_source,
    record_step,
)
from dataproof_runner.artifacts import LocalArtifactStore
from dataproof_runner.backend import (
    EnvironmentHandle,
    ExecutionBackend,
    StagedSource,
    StepResult,
)
from dataproof_runner.baselines import Baseline, database_digest, user_tables
from dataproof_runner.project import ProjectConfig
from dataproof_runner.source import archive_commit, changed_paths
from dataproof_runner.worker import StageContext, StageHandler, StageOutcome
from dataproof_verifier import (
    OracleFile,
    QueryLimits,
    assemble_contract,
    derive_verdict,
    parse_binding,
    parse_oracle,
    verify_contract,
)

MIGRATION_CHECK = "migration.execute"
MIGRATION_CONTRACT = "dataproof.migration_executes"
MIGRATION_STEP = "candidate.migration"
RESTORE_VARIANT = "restore"
EXIT_SOURCE_DIGEST_MISMATCH = 65
_MAX_CONTRACT_FILE = 64 * 1024


@dataclass(frozen=True)
class PipelineSettings:
    environment_ttl: timedelta = timedelta(hours=4)
    """Expiry label on environments: the reaper's safety net if cleanup never runs."""
    evidence_retention: timedelta = timedelta(days=30)
    query_limits: QueryLimits = field(default_factory=QueryLimits)


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

    def handlers(self) -> dict[RunState, StageHandler]:
        return {
            RunState.FETCHING: self.fetch,
            RunState.ANALYZING: self.analyze,
            RunState.PROVISIONING: self.provision,
            RunState.MIGRATING: self.migrate,
            RunState.VERIFYING: self.verify,
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
        return StageOutcome(
            RunState.MIGRATING,
            isolation_id=isolation[0].rsplit("-", 1)[0],  # the run's prefix, without the role
            detail={
                "backend": self.backend.name,
                "environments": isolation,
                "dataset_digest": run.inputs.dataset_digest,
                "database_digest": digests[Environment.REFERENCE.value],
            },
            effects=self._steps(run.run_id, steps),
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
        return StageOutcome(
            RunState.VERIFYING,
            detail={
                "succeeded": result.succeeded,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": step.duration_ms,
            },
            effects=self._steps(run.run_id, [step]),
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
        if migration.succeeded:
            checks += self._contract_checks(run, evidence)
        else:
            checks += [
                CheckRecord(self._not_run(spec, NotRunReason.PREREQUISITE_FAILED, dataset))
                for spec in self._oracle_specs(run)
            ]
        summary = {c.result.check_id: c.result.outcome.value for c in checks}
        return StageOutcome(
            RunState.REPORTING,
            detail={"outcomes": summary},
            effects=_all(
                *(self._recorder(record_check, run.run_id, check) for check in checks),
                *(
                    self._recorder(record_evidence, run.run_id, evidence_id, record)
                    for evidence_id, record in sorted(evidence.items())
                ),
            ),
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
        self, run: ReviewRun, evidence: dict[str, Mapping[str, Any]]
    ) -> list[CheckRecord]:
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

        reference = self._attached(run.run_id, Environment.REFERENCE)
        candidate = self._attached(run.run_id, Environment.CANDIDATE)
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
                verification = verify_contract(
                    contract, ref, cand, dataset, self.settings.query_limits
                )
                for item in verification.evidence:
                    evidence[item.evidence_id] = {
                        "kind": "query",
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
