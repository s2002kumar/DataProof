"""C8, the type check: the candidate's column types can hold what the contract compares.

A data comparison proves only what the fixture exercises. A money column declared
``numeric(12, 0)`` passes a fixture of whole-unit amounts and rounds the first real refund; a
``timestamp without time zone`` column has no instant to compare. This check reads what
PostgreSQL declared the candidate's result columns to be (``RowDescription``) and judges each
compared column against its comparator:

* ``DECIMAL`` with scale ``s``: ``numeric`` (with a declared scale of at least ``s``, when the
  scale is declared) or an integer type when ``s`` is zero. Floating-point types never qualify.
* ``INSTANT``: ``timestamp with time zone``. A ``timestamp``, ``date`` or text column has no
  instant.
* ``EXACT``: no constraint beyond what the values themselves enforce.

A column read through an expression carries no declared scale, so only its type family is
judged; that is stated in the evidence rather than counted as verified. The check is keyed by
contract column, so each failure names the column, what the contract needs and what the
candidate declared.
"""

from collections.abc import Mapping
from typing import Any

from dataproof_contracts import (
    CheckOutcome,
    CheckResult,
    Comparator,
    ComparatorKind,
    DataContract,
    Mismatch,
    NotRunReason,
    TextValue,
)
from dataproof_verifier.engine import Verification
from dataproof_verifier.queries import ColumnType

CHECK_PREFIX = "types."
NUMERIC_OID = 1700
INTEGER_OIDS = frozenset({20, 21, 23})
FLOAT_OIDS = frozenset({700, 701})
TIMESTAMPTZ_OID = 1184


def _needs(comparator: Comparator) -> str:
    if comparator.kind is ComparatorKind.DECIMAL:
        return f"numeric with scale >= {comparator.decimal_scale}"
    if comparator.kind is ComparatorKind.INSTANT:
        return "timestamp with time zone"
    return "any exact type"


def _fits(comparator: Comparator, column: ColumnType) -> bool:
    if comparator.kind is ComparatorKind.DECIMAL:
        scale = comparator.decimal_scale or 0
        if column.oid in INTEGER_OIDS:
            return scale == 0
        if column.oid != NUMERIC_OID:
            return False
        return column.scale is None or column.scale >= scale
    if comparator.kind is ComparatorKind.INSTANT:
        return column.oid == TIMESTAMPTZ_OID
    return column.oid not in FLOAT_OIDS


#: SQL names for the types this check judges; psycopg's display names are shorter (``timestamp``
#: for both timestamp types), and a reviewer needs the distinction spelled out.
_SQL_NAMES = {
    20: "bigint",
    21: "smallint",
    23: "integer",
    700: "real",
    701: "double precision",
    1082: "date",
    1114: "timestamp without time zone",
    1184: "timestamp with time zone",
}


def _declared(column: ColumnType) -> str:
    if column.oid == NUMERIC_OID and column.scale is not None and column.precision is not None:
        return f"numeric({column.precision}, {column.scale})"
    if column.oid == NUMERIC_OID:
        return "numeric (scale not declared: an expression)"
    return _SQL_NAMES.get(column.oid, column.type_display)


def type_problems(
    types: tuple[ColumnType, ...],
    names: Mapping[str, str],
    expected_columns: Mapping[str, Comparator],
) -> list[Mismatch]:
    """Each compared column whose candidate type cannot hold its comparator, in contract order."""
    by_name = {column.name: column for column in types}
    problems = []
    for column, comparator in expected_columns.items():
        candidate = by_name.get(names.get(column, column))
        if candidate is None or _fits(comparator, candidate):
            continue
        problems.append(
            Mismatch(
                key=(TextValue(value=column),),
                column="type",
                expected=TextValue(value=_needs(comparator)),
                observed=TextValue(value=_declared(candidate)),
            )
        )
    return problems


def type_check(contract: DataContract, verification: Verification) -> CheckResult:
    """The C8 check for one contract, from its verification's candidate column types.

    It is ``NOT_RUN: PREREQUISITE_FAILED`` when the candidate query never ran (a binding awaiting
    review, a rejected binding, a coverage gap, a failed query), ``FAIL`` when a compared column's
    type cannot hold its comparator, and ``PASS`` otherwise. It cites the same evidence as the
    data check: the types come from the same result.
    """
    common: dict[str, Any] = {
        "check_id": CHECK_PREFIX + contract.contract_id,
        "contract_id": contract.contract_id,
        "contract_version": contract.version,
        "required": contract.required,
        "dataset_digest": verification.check.dataset_digest,
    }
    types = verification.candidate_types
    if types is None:
        return CheckResult(
            **common,
            outcome=CheckOutcome.NOT_RUN,
            not_run_reason=NotRunReason.PREREQUISITE_FAILED,
        )
    binding = contract.candidate_binding
    names = binding.column_mapping if binding is not None else {}
    problems = type_problems(types, names, contract.oracle.expected_columns)
    evidence = tuple(e for e in verification.check.evidence_ids if ":candidate:" in e)
    return CheckResult(
        **common,
        outcome=CheckOutcome.FAIL if problems else CheckOutcome.PASS,
        expected_key_count=len(contract.oracle.expected_columns),
        observed_key_count=len(contract.oracle.expected_columns),
        mismatch_count=len(problems),
        mismatch_examples=tuple(problems),
        evidence_ids=evidence,
    )
