"""Load oracle and binding files and assemble data contracts (ADR-0005).

Oracles are read from the base SHA; bindings are proposed by the pull request. Both are TOML.
Provenance (repository path and commit) always comes from the caller, never from the file. A
binding file that tries to carry oracle policy (comparators, tolerances, requiredness, keys)
fails validation, because ``BindingFile`` forbids unknown fields.
"""

import tomllib
from collections.abc import Collection
from pathlib import Path

from pydantic import BaseModel, ConfigDict, PositiveInt

from dataproof_contracts import (
    BindingReviewStatus,
    CandidateBinding,
    Comparator,
    DataContract,
    Oracle,
    Severity,
)


class _File(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OracleFile(_File):
    contract_id: str
    version: PositiveInt
    description: str
    required: bool
    severity: Severity
    query_sql: str
    key_columns: tuple[str, ...]
    expected_columns: dict[str, Comparator]
    reference_precondition_sql: str | None = None


class BindingFile(_File):
    contract_id: str
    query_sql: str | None = None
    api_route: str | None = None
    column_mapping: dict[str, str]


def load_oracle(path: Path, *, source_path: str, base_sha: str) -> tuple[OracleFile, Oracle]:
    return parse_oracle(path.read_text("utf-8"), source_path=source_path, base_sha=base_sha)


def load_binding(path: Path, *, source_path: str, head_sha: str) -> tuple[str, CandidateBinding]:
    return parse_binding(path.read_text("utf-8"), source_path=source_path, head_sha=head_sha)


def parse_oracle(text: str, *, source_path: str, base_sha: str) -> tuple[OracleFile, Oracle]:
    """Parse an oracle file's text, for example a member of the base commit's source archive."""
    spec = OracleFile.model_validate(tomllib.loads(text))
    oracle = Oracle(
        source_path=source_path,
        base_sha=base_sha,
        query_sql=spec.query_sql,
        key_columns=spec.key_columns,
        expected_columns=spec.expected_columns,
        reference_precondition_sql=spec.reference_precondition_sql,
    )
    return spec, oracle


def parse_binding(text: str, *, source_path: str, head_sha: str) -> tuple[str, CandidateBinding]:
    """Parse a binding file's text; the text is untrusted data proposed by the pull request."""
    spec = BindingFile.model_validate(tomllib.loads(text))
    binding = CandidateBinding(
        source_path=source_path,
        head_sha=head_sha,
        query_sql=spec.query_sql,
        api_route=spec.api_route,
        column_mapping=spec.column_mapping,
    )
    return spec.contract_id, binding


def assemble_contract(
    spec: OracleFile,
    oracle: Oracle,
    binding: CandidateBinding | None,
    *,
    approved_digests: Collection[str] = (),
    rejected_digests: Collection[str] = (),
) -> DataContract:
    """Bind a binding's review status to its exact digest: any edit needs new review."""
    review = BindingReviewStatus.NOT_REQUIRED
    approved_digest = None
    if binding is not None:
        digest = binding.digest()
        if digest in approved_digests:
            review, approved_digest = BindingReviewStatus.APPROVED, digest
        elif digest in rejected_digests:
            review = BindingReviewStatus.REJECTED
        else:
            review = BindingReviewStatus.PENDING
    return DataContract(
        contract_id=spec.contract_id,
        version=spec.version,
        description=spec.description,
        required=spec.required,
        severity=spec.severity,
        oracle=oracle,
        candidate_binding=binding,
        binding_review=review,
        approved_binding_digest=approved_digest,
    )
