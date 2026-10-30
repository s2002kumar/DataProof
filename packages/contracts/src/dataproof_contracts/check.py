"""Deterministic check results (docs/VERIFICATION.md, "Check outcome taxonomy")."""

from typing import Annotated, Self

from pydantic import Field, NonNegativeInt, PositiveInt, model_validator

from dataproof_contracts._base import Contract, Identifier, NonEmptyText, Sha256Hex
from dataproof_contracts.enums import CheckOutcome, ErrorCode, NotRunReason
from dataproof_contracts.values import TypedValue


class Mismatch(Contract):
    """One keyed difference between expected (reference) and observed (candidate) data.

    ``None`` on a side means the key is absent there; SQL NULL is ``NullValue``. Keys are tuples
    of typed values, never delimited strings.
    """

    key: Annotated[tuple[TypedValue, ...], Field(min_length=1)]
    column: NonEmptyText | None = None
    expected: TypedValue | None
    observed: TypedValue | None

    @model_validator(mode="after")
    def _is_a_difference(self) -> Self:
        if self.expected is None and self.observed is None:
            raise ValueError("a mismatch needs a value on at least one side")
        if self.expected == self.observed:
            raise ValueError("expected and observed are equal; this is not a mismatch")
        return self


class CheckResult(Contract):
    check_id: Identifier
    contract_id: Identifier
    contract_version: PositiveInt
    required: bool
    outcome: CheckOutcome
    not_run_reason: NotRunReason | None = None
    error_code: ErrorCode | None = None
    expected_key_count: NonNegativeInt | None = None
    observed_key_count: NonNegativeInt | None = None
    mismatch_count: NonNegativeInt = 0
    mismatch_examples: tuple[Mismatch, ...] = ()
    """Bounded examples; ``mismatch_count`` is the full count."""
    evidence_ids: tuple[Identifier, ...] = ()
    dataset_digest: Sha256Hex
    elapsed_ms: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _outcome_is_supported_by_fields(self) -> Self:
        outcome = self.outcome
        if (outcome is CheckOutcome.NOT_RUN) != (self.not_run_reason is not None):
            raise ValueError("not_run_reason is required exactly when the outcome is NOT_RUN")
        if (outcome is CheckOutcome.ERROR) != (self.error_code is not None):
            raise ValueError("error_code is required exactly when the outcome is ERROR")
        if len(self.mismatch_examples) > self.mismatch_count:
            raise ValueError("more mismatch examples than mismatches")
        if outcome is CheckOutcome.PASS and self.mismatch_count:
            raise ValueError("a PASS cannot report mismatches")
        if outcome is CheckOutcome.FAIL and not self.evidence_ids:
            raise ValueError("a FAIL must cite deterministic evidence")
        if outcome is CheckOutcome.NOT_RUN and (self.mismatch_count or self.evidence_ids):
            raise ValueError("a check that did not run cannot report mismatches or evidence")
        return self
