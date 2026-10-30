"""Data contracts: protected oracle plus PR-proposed candidate binding (ADR-0005)."""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, PositiveInt, model_validator

from dataproof_contracts._base import (
    Contract,
    GitSha,
    Identifier,
    NonEmptyText,
    Sha256Hex,
    sha256_digest,
)
from dataproof_contracts.enums import BindingReviewStatus, ComparatorKind, Severity
from dataproof_contracts.values import ExactDecimal

ColumnName = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]


class Comparator(Contract):
    kind: ComparatorKind
    decimal_scale: Annotated[int, Field(ge=0, le=38)] | None = None
    # Checked in the validator: Field(ge=0) on a decimal leaks a non-standard "ge" keyword into
    # the JSON Schema, because decimals serialize as strings.
    tolerance: ExactDecimal = Decimal(0)

    @model_validator(mode="after")
    def _decimal_only_options(self) -> Self:
        if self.tolerance < 0:
            raise ValueError("tolerance must be non-negative")
        is_decimal = self.kind is ComparatorKind.DECIMAL
        if is_decimal != (self.decimal_scale is not None):
            raise ValueError("decimal_scale is required for DECIMAL comparators and only for them")
        if not is_decimal and self.tolerance != 0:
            raise ValueError("only DECIMAL comparators may declare a tolerance")
        return self


class Oracle(Contract):
    """Protected expectation, read from the base SHA only.

    It alone fixes the expected key universe, the comparators and their tolerances.
    """

    source_path: NonEmptyText
    base_sha: GitSha
    query_sql: NonEmptyText
    key_columns: Annotated[tuple[ColumnName, ...], Field(min_length=1)]
    expected_columns: Annotated[dict[ColumnName, Comparator], Field(min_length=1)]
    reference_precondition_sql: NonEmptyText | None = None
    """Reference-first rule: a query returning the rows that violate a non-equivalence invariant
    (for example refunds exceeding captures). It runs on the reference before anything else;
    any row means ``ERROR: ORACLE_VIOLATED_ON_REFERENCE``, a fixture or contract defect."""

    @model_validator(mode="after")
    def _keys_are_not_values(self) -> Self:
        overlap = set(self.key_columns) & set(self.expected_columns)
        if overlap:
            raise ValueError(f"columns cannot be both key and compared value: {sorted(overlap)}")
        if len(set(self.key_columns)) != len(self.key_columns):
            raise ValueError("key_columns contains duplicates")
        return self


class CandidateBinding(Contract):
    """Untrusted, PR-supplied read mapping for the candidate schema or API.

    It has no fields for comparators, tolerances, requiredness or key filters, and unknown
    fields are rejected, so a binding cannot weaken the oracle it is checked against.
    """

    source_path: NonEmptyText
    head_sha: GitSha
    query_sql: NonEmptyText | None = None
    api_route: NonEmptyText | None = None
    column_mapping: Annotated[dict[ColumnName, ColumnName], Field(min_length=1)]
    """Oracle column (key or value) -> candidate column."""

    @model_validator(mode="after")
    def _has_a_source(self) -> Self:
        if self.query_sql is None and self.api_route is None:
            raise ValueError("a binding must declare query_sql, api_route or both")
        return self

    def digest(self) -> str:
        return sha256_digest(self)


class DataContract(Contract):
    contract_id: Identifier
    version: PositiveInt
    description: NonEmptyText
    required: bool
    severity: Severity
    oracle: Oracle
    candidate_binding: CandidateBinding | None = None
    binding_review: BindingReviewStatus
    approved_binding_digest: Sha256Hex | None = None

    @model_validator(mode="after")
    def _binding_review_is_bound_to_digest(self) -> Self:
        if self.candidate_binding is None:
            if self.binding_review is not BindingReviewStatus.NOT_REQUIRED:
                raise ValueError("binding_review must be NOT_REQUIRED when there is no binding")
        elif self.binding_review is BindingReviewStatus.NOT_REQUIRED:
            raise ValueError("a candidate binding always requires review")
        approved = self.binding_review is BindingReviewStatus.APPROVED
        if approved != (self.approved_binding_digest is not None):
            raise ValueError("approved_binding_digest is required exactly when APPROVED")
        if (
            approved
            and self.candidate_binding is not None
            and self.approved_binding_digest != self.candidate_binding.digest()
        ):
            raise ValueError("approval does not match this binding's digest; it needs new review")
        return self

    def binding_coverage_gaps(self) -> frozenset[str]:
        """Oracle columns the binding does not map.

        A gap is not silently excluded: the verifier must report the affected check as
        ``ERROR``/``FAIL`` (docs/VERIFICATION.md, contract governance).
        """
        if self.candidate_binding is None:
            return frozenset()
        needed = set(self.oracle.key_columns) | set(self.oracle.expected_columns)
        return frozenset(needed - set(self.candidate_binding.column_mapping))
