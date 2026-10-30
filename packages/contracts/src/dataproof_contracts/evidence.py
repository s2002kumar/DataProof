"""Sealed evidence for one run (docs/ARCHITECTURE.md, ``EvidenceManifest``; ADR-0004)."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, PositiveInt, model_validator

from dataproof_contracts._base import (
    Contract,
    GitSha,
    Identifier,
    NonEmptyText,
    OciDigest,
    Sha256Hex,
)
from dataproof_contracts.enums import CleanupOutcome, Environment


class SourceArtifact(Contract):
    """Staged source archive. Its SHA-256 proves this artifact's integrity, not fetch replay."""

    environment: Environment
    commit_sha: GitSha
    object_key: NonEmptyText
    sha256: Sha256Hex


class SnapshotArtifact(Contract):
    """``pg_dump -Fc`` of one environment's database, taken by the trusted broker."""

    environment: Environment
    format: Literal["pg_dump_custom"] = "pg_dump_custom"
    object_key: NonEmptyText
    sha256: Sha256Hex
    size_bytes: PositiveInt
    captured_at: AwareDatetime


class RestoreVerification(Contract):
    snapshot_sha256: Sha256Hex
    verified_at: AwareDatetime
    restored: bool


class EvidenceRecord(Contract):
    evidence_id: Identifier
    kind: NonEmptyText
    object_key: NonEmptyText
    sha256: Sha256Hex
    created_at: AwareDatetime


class CleanupRecord(Contract):
    resource: NonEmptyText
    action: NonEmptyText
    outcome: CleanupOutcome
    at: AwareDatetime


class EvidenceManifest(Contract):
    run_id: UUID
    input_digest: Sha256Hex
    dataset_digest: Sha256Hex
    config_digest: Sha256Hex
    binding_digest: Sha256Hex | None = None
    image_digests: Annotated[tuple[OciDigest, ...], Field(min_length=1)]
    source_artifacts: tuple[SourceArtifact, ...] = ()
    snapshots: tuple[SnapshotArtifact, ...] = ()
    restore_verifications: tuple[RestoreVerification, ...] = ()
    records: tuple[EvidenceRecord, ...] = ()
    cleanup_log: tuple[CleanupRecord, ...] = ()
    sealed_at: AwareDatetime
    retain_until: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for label, environments in (
            ("source artifact", [a.environment for a in self.source_artifacts]),
            ("snapshot", [s.environment for s in self.snapshots]),
        ):
            if len(set(environments)) != len(environments):
                raise ValueError(f"at most one {label} per environment")
        snapshot_digests = {s.sha256 for s in self.snapshots}
        unknown = {r.snapshot_sha256 for r in self.restore_verifications} - snapshot_digests
        if unknown:
            raise ValueError("restore verification refers to a snapshot not in this manifest")
        ids = [r.evidence_id for r in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError("evidence IDs must be unique")
        if self.retain_until <= self.sealed_at:
            raise ValueError("retain_until must be after sealed_at")
        return self

    def evidence_ids(self) -> frozenset[str]:
        return frozenset(r.evidence_id for r in self.records)
