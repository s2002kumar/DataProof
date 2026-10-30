"""Write-once run results in the control database (migration 0002).

Workers write results through the ``record_*`` functions inside a transition's ``effects``, so
results and the transition that records their stage commit together. :class:`RunResults` reads
them back.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from dataproof_contracts import (
    BindingAnalysis,
    CheckResult,
    Environment,
    EvidenceManifest,
    GitHubCheckConclusion,
    ReportVerdict,
)


@dataclass(frozen=True)
class SourceRecord:
    environment: Environment
    commit_sha: str
    sha256: str
    size_bytes: int
    object_key: str


@dataclass(frozen=True)
class StepRecord:
    step_id: str
    environment: Environment
    kind: str
    source_sha256: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output: str

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class CheckRecord:
    result: CheckResult
    binding_analysis: BindingAnalysis | None = None
    binding_problem: str | None = None


@dataclass(frozen=True)
class SnapshotRecord:
    """A sealed ``pg_dump`` of one environment and whether it restored to the same rows."""

    environment: Environment
    object_key: str
    sha256: str
    size_bytes: int
    captured_at: datetime
    database_digest: str
    restored_digest: str | None
    verified_at: datetime
    restore_error: str | None = None

    @property
    def restore_verified(self) -> bool:
        return self.restored_digest == self.database_digest


@dataclass(frozen=True)
class ManifestRecord:
    object_key: str
    sha256: str
    manifest: EvidenceManifest


@dataclass(frozen=True)
class ReportRecord:
    verdict: ReportVerdict
    conclusion: GitHubCheckConclusion
    report: Mapping[str, Any]


def record_source(conn: psycopg.Connection[Any], run_id: UUID, source: SourceRecord) -> None:
    conn.execute(
        "INSERT INTO run_sources (run_id, environment, commit_sha, sha256, size_bytes, object_key) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            run_id,
            source.environment.value,
            source.commit_sha,
            source.sha256,
            source.size_bytes,
            source.object_key,
        ),
    )


def record_step(conn: psycopg.Connection[Any], run_id: UUID, step: StepRecord) -> None:
    conn.execute(
        "INSERT INTO run_steps (run_id, step_id, environment, kind, source_sha256, exit_code, "
        "timed_out, duration_ms, output) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            step.step_id,
            step.environment.value,
            step.kind,
            step.source_sha256,
            step.exit_code,
            step.timed_out,
            step.duration_ms,
            step.output,
        ),
    )


def record_check(conn: psycopg.Connection[Any], run_id: UUID, check: CheckRecord) -> None:
    result = check.result
    conn.execute(
        "INSERT INTO run_checks (run_id, check_id, required, outcome, result, binding_analysis, "
        "binding_problem) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            result.check_id,
            result.required,
            result.outcome.value,
            Jsonb(result.model_dump(mode="json")),
            Jsonb(check.binding_analysis.model_dump(mode="json"))
            if check.binding_analysis
            else None,
            check.binding_problem,
        ),
    )


def record_evidence(
    conn: psycopg.Connection[Any], run_id: UUID, evidence_id: str, record: Mapping[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO run_evidence (run_id, evidence_id, record) VALUES (%s, %s, %s)",
        (run_id, evidence_id, Jsonb(dict(record))),
    )


def record_snapshot(conn: psycopg.Connection[Any], run_id: UUID, snapshot: SnapshotRecord) -> None:
    conn.execute(
        "INSERT INTO run_snapshots (run_id, environment, object_key, sha256, size_bytes, "
        "captured_at, database_digest, restored_digest, restore_verified, verified_at, "
        "restore_error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            snapshot.environment.value,
            snapshot.object_key,
            snapshot.sha256,
            snapshot.size_bytes,
            snapshot.captured_at,
            snapshot.database_digest,
            snapshot.restored_digest,
            snapshot.restore_verified,
            snapshot.verified_at,
            snapshot.restore_error,
        ),
    )


def record_manifest(conn: psycopg.Connection[Any], run_id: UUID, manifest: ManifestRecord) -> None:
    conn.execute(
        "INSERT INTO run_manifests (run_id, object_key, sha256, manifest, sealed_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            run_id,
            manifest.object_key,
            manifest.sha256,
            Jsonb(manifest.manifest.model_dump(mode="json")),
            manifest.manifest.sealed_at,
        ),
    )


def record_report(conn: psycopg.Connection[Any], run_id: UUID, report: ReportRecord) -> None:
    conn.execute(
        "INSERT INTO run_reports (run_id, verdict, conclusion, report) VALUES (%s, %s, %s, %s)",
        (run_id, report.verdict.value, report.conclusion.value, Jsonb(dict(report.report))),
    )


class RunResults:
    """Reads a run's recorded results."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _rows(self, query: str, run_id: UUID) -> list[dict[str, Any]]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as conn:
            return conn.execute(query, (run_id,)).fetchall()

    def sources(self, run_id: UUID) -> dict[Environment, SourceRecord]:
        rows = self._rows(
            "SELECT environment, commit_sha, sha256, size_bytes, object_key "
            "FROM run_sources WHERE run_id = %s",
            run_id,
        )
        return {
            Environment(row["environment"]): SourceRecord(
                environment=Environment(row["environment"]),
                commit_sha=row["commit_sha"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                object_key=row["object_key"],
            )
            for row in rows
        }

    def steps(self, run_id: UUID) -> list[StepRecord]:
        rows = self._rows(
            "SELECT step_id, environment, kind, source_sha256, exit_code, timed_out, duration_ms, "
            "output FROM run_steps WHERE run_id = %s ORDER BY recorded_at, step_id",
            run_id,
        )
        return [
            StepRecord(
                step_id=row["step_id"],
                environment=Environment(row["environment"]),
                kind=row["kind"],
                source_sha256=row["source_sha256"],
                exit_code=row["exit_code"],
                timed_out=row["timed_out"],
                duration_ms=row["duration_ms"],
                output=row["output"],
            )
            for row in rows
        ]

    def checks(self, run_id: UUID) -> list[CheckRecord]:
        rows = self._rows(
            "SELECT result, binding_analysis, binding_problem FROM run_checks "
            "WHERE run_id = %s ORDER BY check_id",
            run_id,
        )
        return [
            CheckRecord(
                result=CheckResult.model_validate(row["result"]),
                binding_analysis=BindingAnalysis.model_validate(row["binding_analysis"])
                if row["binding_analysis"]
                else None,
                binding_problem=row["binding_problem"],
            )
            for row in rows
        ]

    def evidence(self, run_id: UUID) -> dict[str, Mapping[str, Any]]:
        rows = self._rows(
            "SELECT evidence_id, record FROM run_evidence WHERE run_id = %s ORDER BY evidence_id",
            run_id,
        )
        return {row["evidence_id"]: row["record"] for row in rows}

    def snapshots(self, run_id: UUID) -> dict[Environment, SnapshotRecord]:
        rows = self._rows(
            "SELECT environment, object_key, sha256, size_bytes, captured_at, database_digest, "
            "restored_digest, verified_at, restore_error FROM run_snapshots WHERE run_id = %s",
            run_id,
        )
        return {
            Environment(row["environment"]): SnapshotRecord(
                environment=Environment(row["environment"]),
                object_key=row["object_key"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                captured_at=row["captured_at"],
                database_digest=row["database_digest"],
                restored_digest=row["restored_digest"],
                verified_at=row["verified_at"],
                restore_error=row["restore_error"],
            )
            for row in rows
        }

    def manifest(self, run_id: UUID) -> ManifestRecord | None:
        rows = self._rows(
            "SELECT object_key, sha256, manifest FROM run_manifests WHERE run_id = %s", run_id
        )
        if not rows:
            return None
        (row,) = rows
        return ManifestRecord(
            object_key=row["object_key"],
            sha256=row["sha256"],
            manifest=EvidenceManifest.model_validate(row["manifest"]),
        )

    def report(self, run_id: UUID) -> ReportRecord | None:
        rows = self._rows(
            "SELECT verdict, conclusion, report FROM run_reports WHERE run_id = %s", run_id
        )
        if not rows:
            return None
        (row,) = rows
        return ReportRecord(
            verdict=ReportVerdict(row["verdict"]),
            conclusion=GitHubCheckConclusion(row["conclusion"]),
            report=row["report"],
        )
