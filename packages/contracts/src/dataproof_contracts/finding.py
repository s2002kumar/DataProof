"""Findings reported to reviewers (docs/ARCHITECTURE.md, ``Finding``)."""

from typing import Annotated, Self

from pydantic import Field, PositiveInt, model_validator

from dataproof_contracts._base import Contract, GitSha, Identifier, NonEmptyText
from dataproof_contracts.check import Mismatch
from dataproof_contracts.enums import FindingKind, LocalizationBasis, Severity


class SourceLocation(Contract):
    path: NonEmptyText
    commit_sha: GitSha
    start_line: PositiveInt
    end_line: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("end_line precedes start_line")
        return self


class Finding(Contract):
    finding_id: Identifier
    kind: FindingKind
    severity: Severity
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    affected_objects: tuple[NonEmptyText, ...] = ()
    examples: tuple[Mismatch, ...] = ()
    redacted: bool
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    source_location: SourceLocation | None = None
    localization: LocalizationBasis
    check_id: Identifier | None = None
    """The deterministic check this finding reports; only static-risk findings may lack one."""

    @model_validator(mode="after")
    def _non_static_findings_cite_a_check(self) -> Self:
        if self.kind is not FindingKind.STATIC_RISK and self.check_id is None:
            raise ValueError(f"{self.kind} findings must reference the deterministic check")
        if self.source_location is None and self.localization is not LocalizationBasis.UNKNOWN:
            raise ValueError("localization must be UNKNOWN when there is no source location")
        return self
