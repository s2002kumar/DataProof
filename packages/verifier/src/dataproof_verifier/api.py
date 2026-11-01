"""API behaviour checks (SCENARIO_CONTRACT, C6): the reference API against the candidate API.

An API oracle's ``query_sql`` runs on the reference database only and fixes the key universe:
each row fills the placeholders of the oracle's reference route, and of the binding's candidate
route. Every response (an object, or a list of objects) becomes rows that also carry the row's
columns. Both sides are then keyed and compared with the oracle's typed comparators, exactly as
data contracts are, so a candidate cannot narrow the universe by returning fewer items.

Requests go only through an :class:`ApiClient` bound to one preview; routes are local paths
(``ApiRoute``), so a binding cannot send a request anywhere else.
"""

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import quote

import psycopg

from dataproof_contracts import (
    BindingReviewStatus,
    CheckOutcome,
    CheckResult,
    ComparatorKind,
    DataContract,
    Environment,
    ErrorCode,
    NotRunReason,
)
from dataproof_verifier.compare import compare_keyed
from dataproof_verifier.engine import MAX_EXAMPLES, QueryEvidence, Verification, _CheckError, _Run
from dataproof_verifier.queries import DEFAULT_LIMITS, QueryLimits, sql_digest

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
DEFAULT_MAX_REQUESTS = 5000


class ApiRequestError(Exception):
    """A request to a preview's API failed (status, transport or non-JSON body)."""


class ApiClient(Protocol):
    def get(self, path: str) -> Any:
        """GET a local path on one preview's API and return the parsed JSON body."""
        ...


def _render(route: str, row: Mapping[str, Any]) -> str:
    def value(match: re.Match[str]) -> str:
        return quote(str(row[match.group(1)]), safe="")

    return _PLACEHOLDER.sub(value, route)


def _placeholders(route: str) -> set[str]:
    return set(_PLACEHOLDER.findall(route))


def _typed(column: str, raw: Any, kind: ComparatorKind | None) -> object:
    """Convert a JSON value for comparison; floats are never exact enough for money or keys."""
    if isinstance(raw, float):
        raise _CheckError(ErrorCode.UNSUPPORTED_VALUE_TYPE, f"{column} is a JSON float")
    if raw is None or kind is None or kind is ComparatorKind.EXACT:
        return raw
    if kind is ComparatorKind.DECIMAL:
        if isinstance(raw, bool) or not isinstance(raw, str | int):
            raise _CheckError(ErrorCode.UNSUPPORTED_VALUE_TYPE, f"{column} is not a decimal")
        try:
            return Decimal(str(raw))
        except InvalidOperation as exc:
            raise _CheckError(ErrorCode.UNSUPPORTED_VALUE_TYPE, f"{column}: {raw!r}") from exc
    if not isinstance(raw, str):
        raise _CheckError(ErrorCode.UNSUPPORTED_VALUE_TYPE, f"{column} is not an instant")
    instant = datetime.fromisoformat(raw)
    if instant.tzinfo is None:
        raise _CheckError(ErrorCode.UNSUPPORTED_VALUE_TYPE, f"{column} has no UTC offset")
    return instant


def _fetch_rows(
    client: ApiClient,
    route: str,
    universe: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    contract: DataContract,
    max_requests: int,
) -> tuple[dict[tuple[object, ...], tuple[object, ...]], str]:
    """Call ``route`` for every row of the universe; key the responses by the oracle's keys."""
    oracle = contract.oracle
    if len(universe) > max_requests:
        raise _CheckError(
            ErrorCode.RESULT_LIMIT_EXCEEDED, f"{len(universe)} requests exceed {max_requests}"
        )
    value_columns = tuple(oracle.expected_columns)
    keyed: dict[tuple[object, ...], tuple[object, ...]] = {}
    digest_rows: list[str] = []
    for parameters in universe:
        path = _render(route, parameters)
        try:
            body = client.get(path)
        except ApiRequestError as exc:
            raise _CheckError(ErrorCode.API_REQUEST_FAILED, f"GET {path}: {exc}") from exc
        items = body if isinstance(body, list) else [body]
        for item in items:
            if not isinstance(item, dict):
                raise _CheckError(ErrorCode.API_REQUEST_FAILED, f"GET {path}: not JSON objects")
            row = {**parameters, **item}
            missing = [
                names[c] for c in (*oracle.key_columns, *value_columns) if names[c] not in row
            ]
            if missing:
                raise _CheckError(ErrorCode.BINDING_COVERAGE_GAP, f"responses lack {missing}")
            key = tuple(_typed(c, row[names[c]], None) for c in oracle.key_columns)
            if key in keyed:
                raise _CheckError(ErrorCode.DUPLICATE_KEY, f"duplicate key {key!r}")
            keyed[key] = tuple(
                _typed(c, row[names[c]], oracle.expected_columns[c].kind) for c in value_columns
            )
            digest_rows.append(json.dumps(row, sort_keys=True, default=str))
    digest = hashlib.sha256("\n".join(sorted(digest_rows)).encode()).hexdigest()
    return keyed, digest


def verify_api_contract(
    contract: DataContract,
    reference_database: psycopg.Connection[Any],
    reference_api: ApiClient,
    candidate_api: ApiClient,
    dataset_digest: str,
    limits: QueryLimits = DEFAULT_LIMITS,
    max_requests: int = DEFAULT_MAX_REQUESTS,
) -> Verification:
    """Compare the reference API with the candidate API under an API oracle."""
    oracle = contract.oracle
    if oracle.api_route is None:
        raise ValueError(f"{contract.contract_id} is not an API contract")
    started = time.monotonic()
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
    problem: str | None = None
    if binding is not None and binding.api_route is None:
        problem = "an API contract needs a binding with an api_route"
    if binding is not None and contract.binding_review is not BindingReviewStatus.APPROVED:
        rejected = contract.binding_review is BindingReviewStatus.REJECTED
        reason = NotRunReason.BINDING_REJECTED if rejected else NotRunReason.AWAITING_BINDING_REVIEW
        return Verification(
            CheckResult(**common, outcome=CheckOutcome.NOT_RUN, not_run_reason=reason),
            (),
            binding_problem=problem,
        )

    run = _Run(contract, limits)
    evidence: list[QueryEvidence] = []
    try:
        if problem is not None:
            raise _CheckError(ErrorCode.BINDING_UNSUPPORTED_CONSTRUCT, problem)
        if contract.binding_coverage_gaps():
            raise _CheckError(
                ErrorCode.BINDING_COVERAGE_GAP,
                f"unmapped: {sorted(contract.binding_coverage_gaps())}",
            )
        if oracle.reference_precondition_sql is not None:
            violations = run.query(
                reference_database,
                Environment.REFERENCE,
                "precondition",
                oracle.reference_precondition_sql,
            )
            if violations.rows:
                raise _CheckError(
                    ErrorCode.ORACLE_VIOLATED_ON_REFERENCE, f"{len(violations.rows)} violating rows"
                )
        keys = run.query(reference_database, Environment.REFERENCE, "universe", oracle.query_sql)
        universe = [dict(zip(keys.columns, row, strict=True)) for row in keys.rows]
        candidate_route = binding.api_route if binding and binding.api_route else oracle.api_route
        for route, side in ((oracle.api_route, "oracle"), (candidate_route, "binding")):
            unknown = _placeholders(route) - set(keys.columns)
            if unknown:
                code = (
                    ErrorCode.BINDING_UNSUPPORTED_CONSTRUCT
                    if side == "binding"
                    else ErrorCode.QUERY_FAILED
                )
                raise _CheckError(code, f"{route} uses unknown placeholders {sorted(unknown)}")
        identity = {c: c for c in (*oracle.key_columns, *oracle.expected_columns)}
        names = binding.column_mapping if binding is not None else identity
        sides: Iterable[tuple[Environment, ApiClient, str, Mapping[str, str]]] = (
            (Environment.REFERENCE, reference_api, oracle.api_route, identity),
            (Environment.CANDIDATE, candidate_api, candidate_route, names),
        )
        keyed = []
        for environment, client, route, mapping in sides:
            rows, digest = _fetch_rows(client, route, universe, mapping, contract, max_requests)
            keyed.append(rows)
            evidence.append(
                QueryEvidence(
                    evidence_id=f"{contract.contract_id}:{environment.value.lower()}:api",
                    environment=environment,
                    query_sql=f"GET {route}",
                    sql_sha256=sql_digest(f"GET {route}"),
                    result_sha256=digest,
                    row_count=len(rows),
                )
            )
        expected, observed = keyed
        value_columns = tuple(oracle.expected_columns)
        try:
            mismatches = compare_keyed(expected, observed, value_columns, oracle.expected_columns)
        except (TypeError, ValueError) as exc:
            raise _CheckError(ErrorCode.UNSUPPORTED_VALUE_TYPE, str(exc)) from exc
    except _CheckError as stop:
        all_evidence = (*run.evidence, *evidence)
        check = CheckResult(
            **common,
            outcome=CheckOutcome.ERROR,
            error_code=stop.code,
            evidence_ids=tuple(e.evidence_id for e in all_evidence),
            elapsed_ms=elapsed(),
        )
        return Verification(check, tuple(all_evidence), binding_problem=problem)

    all_evidence = (*run.evidence, *evidence)
    check = CheckResult(
        **common,
        outcome=CheckOutcome.FAIL if mismatches else CheckOutcome.PASS,
        expected_key_count=len(expected),
        observed_key_count=len(observed),
        mismatch_count=len(mismatches),
        mismatch_examples=tuple(mismatches[:MAX_EXAMPLES]),
        evidence_ids=tuple(e.evidence_id for e in all_evidence),
        elapsed_ms=elapsed(),
    )
    return Verification(check, tuple(all_evidence))
