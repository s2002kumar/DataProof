"""What a reproduction starts from, and what counts as reproducing it (#8).

A failure's signature is its first ``SEED_KEYS`` failing keys, in a fixed order, each with the
columns that differed. A replay reproduces the failure only if the check fails again and every
seed key fails on the same columns. Anything less is ``NOT_REPRODUCED`` with the reason. The
pipeline (``Pipeline.reproduce``) and the benchmark apply exactly these rules.
"""

import json
from collections.abc import Sequence
from typing import Any

from dataproof_contracts import CheckOutcome, CheckResult, FailureKey, ReproductionStatus

SEED_KEYS = 3
"""How many failing keys a reproduction starts from: the first ones, in a fixed order."""


def _encoded(key: Sequence[Any]) -> str:
    return json.dumps([k.model_dump(mode="json") for k in key], sort_keys=True)


def _failing_columns(result: CheckResult) -> tuple[dict[str, Any], dict[str, set[str]]]:
    keys: dict[str, Any] = {}
    columns: dict[str, set[str]] = {}
    for mismatch in result.mismatch_examples:
        encoded = _encoded(mismatch.key)
        keys[encoded] = mismatch.key
        columns.setdefault(encoded, set())
        if (
            mismatch.column is not None
            and mismatch.expected is not None
            and mismatch.observed is not None
        ):
            columns[encoded].add(mismatch.column)
    return keys, columns


def failure_signature(result: CheckResult) -> list[FailureKey]:
    """The first ``SEED_KEYS`` failing keys, in a fixed order, with their differing columns."""
    keys, columns = _failing_columns(result)
    return [
        FailureKey(key=keys[encoded], columns=tuple(sorted(columns[encoded])))
        for encoded in sorted(keys)[:SEED_KEYS]
    ]


def unconfirmed(signature: Sequence[FailureKey], result: CheckResult) -> list[FailureKey]:
    """Seed keys the result does not show failing on the same columns."""
    _, seen = _failing_columns(result)
    return [key for key in signature if seen.get(_encoded(key.key)) != set(key.columns)]


def verdict(
    signature: Sequence[FailureKey], result: CheckResult
) -> tuple[ReproductionStatus, str | None]:
    """``REPRODUCED``, or ``NOT_REPRODUCED`` with the reason."""
    missing = unconfirmed(signature, result)
    if result.outcome is CheckOutcome.FAIL and not missing:
        return ReproductionStatus.REPRODUCED, None
    if result.outcome is not CheckOutcome.FAIL:
        return (
            ReproductionStatus.NOT_REPRODUCED,
            f"the check is {result.outcome.value} on the subset",
        )
    return (
        ReproductionStatus.NOT_REPRODUCED,
        f"{len(missing)} seed key(s) did not fail the same way on the subset",
    )


def seeds(signature: Sequence[FailureKey]) -> list[tuple[Any, ...]]:
    """The seed keys as plain values, for selecting the subset."""
    return [tuple(getattr(k, "value", None) for k in key.key) for key in signature]
