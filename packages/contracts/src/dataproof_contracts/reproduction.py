"""Reproductions (VERIFICATION tier 6; ARCHITECTURE, ``Reproduction``).

A reproduction replays one failed check on a small, fixed subset of the baseline in fresh
environments. It is ``REPRODUCED`` only if every seed key fails again, in the same check and on
the same columns; anything less is ``NOT_REPRODUCED`` with the reason. The subset is chosen by
a fixed rule, never widened until a failure happens to recur.
"""

from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, NonNegativeInt, model_validator

from dataproof_contracts._base import Contract, Identifier, NonEmptyText, Sha256Hex
from dataproof_contracts.check import CheckResult
from dataproof_contracts.enums import CheckOutcome
from dataproof_contracts.values import TypedValue


class ReproductionStatus(StrEnum):
    REPRODUCED = "REPRODUCED"
    NOT_REPRODUCED = "NOT_REPRODUCED"


class FailureKey(Contract):
    """One failing key of the original check, and the columns that differed for it."""

    key: Annotated[tuple[TypedValue, ...], Field(min_length=1)]
    columns: tuple[NonEmptyText, ...]
    """Empty when the key was missing from one side altogether."""


class SubsetTable(Contract):
    table: NonEmptyText
    rows: NonNegativeInt


class Reproduction(Contract):
    reproduction_id: UUID
    run_id: UUID
    check_id: Identifier
    status: ReproductionStatus
    signature: Annotated[tuple[FailureKey, ...], Field(min_length=1)]
    """The seed keys: the original failure this reproduction tries to replay."""
    seed_table: NonEmptyText | None
    """The relation that holds the oracle's keys, where the subset starts."""
    subset: tuple[SubsetTable, ...] = ()
    subset_sha256: Sha256Hex | None = None
    object_key: NonEmptyText | None = None
    """Where the subset itself is stored, so the reproduction can be rerun by anyone."""
    result: CheckResult | None = None
    """The check as it came out on the subset."""
    reason: NonEmptyText | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        reproduced = self.status is ReproductionStatus.REPRODUCED
        if reproduced != (self.reason is None):
            raise ValueError("reason is required exactly when the failure did not reproduce")
        if reproduced:
            if self.result is None or self.result.outcome is not CheckOutcome.FAIL:
                raise ValueError("a reproduced failure needs the failing result on the subset")
            if self.subset_sha256 is None or not self.subset:
                raise ValueError("a reproduced failure needs the subset and its digest")
        if self.result is not None and self.result.check_id != self.check_id:
            raise ValueError("the result belongs to another check")
        return self
