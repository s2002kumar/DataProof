"""Evaluate one data contract: protected oracle on the reference, reviewed binding on the candidate.

The order is fixed and every exit is a typed outcome (docs/VERIFICATION.md, ADR-0005):

0. The binding is analyzed first (ADR-0005 review aids), so a binding awaiting review already
   carries what the reviewer needs. Nothing in it executes until it is approved.
1. A binding without a digest-bound approval: ``NOT_RUN`` (awaiting review, or rejected).
1b. An approved binding that is not a single allowed read-only query:
    ``ERROR: BINDING_UNSUPPORTED_CONSTRUCT``.
2. Oracle columns the binding does not map: ``ERROR: BINDING_COVERAGE_GAP``.
3. The reference-first precondition finds violations: ``ERROR: ORACLE_VIOLATED_ON_REFERENCE``.
4. Query failure, limits, duplicate keys or inexact values: ``ERROR`` with the specific code.
5. Otherwise a keyed comparison: ``PASS`` with zero mismatches, or ``FAIL`` with evidence.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from dataproof_contracts import (
    BindingAnalysis,
    BindingReviewStatus,
    CheckOutcome,
    CheckResult,
    DataContract,
    Environment,
    ErrorCode,
    NotRunReason,
)
from dataproof_verifier.binding_review import BindingRejectedError, analyze_binding
from dataproof_verifier.compare import Key, Row, compare_keyed
from dataproof_verifier.queries import (
    DEFAULT_LIMITS,
    QueryError,
    QueryLimits,
    QueryResult,
    run_read_only,
)

MAX_EXAMPLES = 20


@dataclass(frozen=True)
class QueryEvidence:
    """What was run where, and digests of what came back; the evidence store persists it."""

    evidence_id: str
    environment: Environment
    query_sql: str
    sql_sha256: str
    result_sha256: str
    row_count: int


@dataclass(frozen=True)
class Verification:
    check: CheckResult
    evidence: tuple[QueryEvidence, ...]
    binding_analysis: BindingAnalysis | None = None
    """What the binding reads, for the reviewer; present whenever the binding could be analyzed."""
    binding_problem: str | None = None
    """Why the binding is unacceptable, if it is; shown to the reviewer before any approval."""


class _CheckError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Run:
    def __init__(self, contract: DataContract, limits: QueryLimits) -> None:
        self.contract = contract
        self.limits = limits
        self.evidence: list[QueryEvidence] = []

    def query(
        self, connection: psycopg.Connection[Any], environment: Environment, kind: str, sql: str
    ) -> QueryResult:
        try:
            result = run_read_only(connection, sql, self.limits)
        except QueryError as exc:
            raise _CheckError(exc.code, str(exc)) from exc
        self.evidence.append(
            QueryEvidence(
                evidence_id=f"{self.contract.contract_id}:{environment.value.lower()}:{kind}",
                environment=environment,
                query_sql=sql,
                sql_sha256=result.sql_sha256,
                result_sha256=result.result_sha256,
                row_count=len(result.rows),
            )
        )
        return result


def _keyed(
    result: QueryResult,
    key_columns: Sequence[str],
    value_columns: Sequence[str],
    names: Mapping[str, str],
) -> dict[Key, Row]:
    """Index rows by key, reading each oracle column under its (possibly mapped) name."""
    position = {name: index for index, name in enumerate(result.columns)}
    missing = [names[c] for c in (*key_columns, *value_columns) if names[c] not in position]
    if missing:
        raise _CheckError(ErrorCode.BINDING_COVERAGE_GAP, f"result lacks columns: {missing}")
    keyed: dict[Key, Row] = {}
    for row in result.rows:
        key = tuple(row[position[names[c]]] for c in key_columns)
        if key in keyed:
            raise _CheckError(ErrorCode.DUPLICATE_KEY, f"duplicate key {key!r}")
        keyed[key] = tuple(row[position[names[c]]] for c in value_columns)
    return keyed


def verify_contract(
    contract: DataContract,
    reference: psycopg.Connection[Any],
    candidate: psycopg.Connection[Any],
    dataset_digest: str,
    limits: QueryLimits = DEFAULT_LIMITS,
) -> Verification:
    started = time.monotonic()
    run = _Run(contract, limits)
    common: dict[str, Any] = {
        "check_id": contract.contract_id,
        "contract_id": contract.contract_id,
        "contract_version": contract.version,
        "required": contract.required,
        "dataset_digest": dataset_digest,
    }

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    binding = contract.candidate_binding
    analysis: BindingAnalysis | None = None
    problem: str | None = None
    if binding is not None and binding.query_sql is not None:
        try:
            analysis = analyze_binding(binding, reference, candidate, limits)
        except BindingRejectedError as exc:
            problem = str(exc)
        except QueryError as exc:
            check = CheckResult(
                **common, outcome=CheckOutcome.ERROR, error_code=exc.code, elapsed_ms=elapsed()
            )
            return Verification(check, ())

    if binding is not None and contract.binding_review is not BindingReviewStatus.APPROVED:
        rejected = contract.binding_review is BindingReviewStatus.REJECTED
        reason = NotRunReason.BINDING_REJECTED if rejected else NotRunReason.AWAITING_BINDING_REVIEW
        return Verification(
            CheckResult(**common, outcome=CheckOutcome.NOT_RUN, not_run_reason=reason),
            (),
            binding_analysis=analysis,
            binding_problem=problem,
        )
    if problem is not None:
        check = CheckResult(
            **common,
            outcome=CheckOutcome.ERROR,
            error_code=ErrorCode.BINDING_UNSUPPORTED_CONSTRUCT,
            elapsed_ms=elapsed(),
        )
        return Verification(check, (), binding_analysis=analysis, binding_problem=problem)
    if binding is not None and binding.query_sql is None:
        unsupported = NotRunReason.UNSUPPORTED  # API-only bindings are evaluated by behavior checks
        return Verification(
            CheckResult(**common, outcome=CheckOutcome.NOT_RUN, not_run_reason=unsupported), ()
        )

    oracle = contract.oracle
    try:
        if contract.binding_coverage_gaps():
            raise _CheckError(
                ErrorCode.BINDING_COVERAGE_GAP,
                f"unmapped: {sorted(contract.binding_coverage_gaps())}",
            )
        if oracle.reference_precondition_sql is not None:
            violations = run.query(
                reference, Environment.REFERENCE, "precondition", oracle.reference_precondition_sql
            )
            if violations.rows:
                raise _CheckError(
                    ErrorCode.ORACLE_VIOLATED_ON_REFERENCE, f"{len(violations.rows)} violating rows"
                )
        value_columns = tuple(oracle.expected_columns)
        identity = {c: c for c in (*oracle.key_columns, *value_columns)}
        expected = _keyed(
            run.query(reference, Environment.REFERENCE, "oracle", oracle.query_sql),
            oracle.key_columns,
            value_columns,
            identity,
        )
        names = binding.column_mapping if binding is not None else identity
        candidate_sql = (
            binding.query_sql if binding is not None and binding.query_sql else oracle.query_sql
        )
        observed = _keyed(
            run.query(
                candidate, Environment.CANDIDATE, "binding" if binding else "oracle", candidate_sql
            ),
            oracle.key_columns,
            value_columns,
            names,
        )
        try:
            mismatches = compare_keyed(expected, observed, value_columns, oracle.expected_columns)
        except (TypeError, ValueError) as exc:
            raise _CheckError(ErrorCode.UNSUPPORTED_VALUE_TYPE, str(exc)) from exc
    except _CheckError as stop:
        check = CheckResult(
            **common,
            outcome=CheckOutcome.ERROR,
            error_code=stop.code,
            evidence_ids=tuple(e.evidence_id for e in run.evidence),
            elapsed_ms=elapsed(),
        )
        return Verification(
            check, tuple(run.evidence), binding_analysis=analysis, binding_problem=problem
        )

    check = CheckResult(
        **common,
        outcome=CheckOutcome.FAIL if mismatches else CheckOutcome.PASS,
        expected_key_count=len(expected),
        observed_key_count=len(observed),
        mismatch_count=len(mismatches),
        mismatch_examples=tuple(mismatches[:MAX_EXAMPLES]),
        evidence_ids=tuple(e.evidence_id for e in run.evidence),
        elapsed_ms=elapsed(),
    )
    return Verification(
        check, tuple(run.evidence), binding_analysis=analysis, binding_problem=problem
    )
