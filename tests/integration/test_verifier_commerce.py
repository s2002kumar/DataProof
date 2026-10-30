"""The verifier against real PostgreSQL, using the commerce oracles and change-set bindings.

Read-only environments (reference, safe candidate, join candidate) are built once per module;
tests that change a database get their own.
"""

from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from commerce_env import (
    CHANGES,
    JOIN_CHANGE,
    ORACLES,
    SAFE_CHANGE,
    DatabaseFactory,
    baseline,
    candidate,
    database_factory,
    scalar,
)
from commerce_scenario import PUBLISHED_DIGEST, build_fixture
from dataproof_contracts import (
    CandidateBinding,
    CheckOutcome,
    DataContract,
    ErrorCode,
    GitHubCheckConclusion,
    Mismatch,
    NotRunReason,
    ReportVerdict,
    github_check_conclusion,
    typed_value,
)
from dataproof_verifier import (
    QueryError,
    QueryLimits,
    Verification,
    assemble_contract,
    derive_verdict,
    load_binding,
    load_oracle,
    run_read_only,
    verify_contract,
)

pytestmark = pytest.mark.integration

C1, C2, C3 = (
    "commerce.c1_event_preservation",
    "commerce.c2_refund_totals",
    "commerce.c3_capture_totals",
)
BASE_SHA, HEAD_SHA = "b" * 40, "c" * 40


@pytest.fixture(scope="module")
def envs(postgres_dsn: str) -> Iterator[dict[str, str]]:
    with database_factory(postgres_dsn) as create:
        yield {
            "reference": baseline(create),
            "safe": candidate(create, SAFE_CHANGE),
            "join": candidate(create, JOIN_CHANGE),
        }


def contracts(
    change: str,
    *,
    approve: bool = True,
    reject: bool = False,
    overrides: dict[str, CandidateBinding] | None = None,
) -> list[DataContract]:
    """The commerce contracts with the change set's bindings, reviewed as requested."""
    result = []
    for oracle_path in sorted(ORACLES.glob("*.toml")):
        spec, oracle = load_oracle(oracle_path, source_path=oracle_path.name, base_sha=BASE_SHA)
        binding_path = CHANGES / change / "bindings" / oracle_path.name
        contract_id, binding = load_binding(
            binding_path, source_path=binding_path.name, head_sha=HEAD_SHA
        )
        assert contract_id == spec.contract_id
        binding = (overrides or {}).get(contract_id, binding)
        digests = {binding.digest()}
        result.append(
            assemble_contract(
                spec,
                oracle,
                binding,
                approved_digests=digests if approve and not reject else (),
                rejected_digests=digests if reject else (),
            )
        )
    return result


def verify(
    contract_list: list[DataContract],
    reference: str,
    candidate_dsn: str,
    limits: QueryLimits | None = None,
) -> dict[str, Verification]:
    with (
        psycopg.connect(reference, autocommit=True) as ref,
        psycopg.connect(candidate_dsn, autocommit=True) as cand,
    ):
        return {
            c.contract_id: verify_contract(c, ref, cand, PUBLISHED_DIGEST, limits or QueryLimits())
            for c in contract_list
        }


def override(
    contract_id: str, query_sql: str, **mapping_changes: Any
) -> dict[str, CandidateBinding]:
    name = contract_id.split(".")[1] + ".toml"
    _, binding = load_binding(
        CHANGES / SAFE_CHANGE / "bindings" / name, source_path=name, head_sha=HEAD_SHA
    )
    mapping = {**binding.column_mapping, **mapping_changes}
    mapping = {k: v for k, v in mapping.items() if v is not None}
    return {
        contract_id: binding.model_copy(update={"query_sql": query_sql, "column_mapping": mapping})
    }


def test_safe_change_is_checked(envs: dict[str, str]) -> None:
    results = verify(contracts(SAFE_CHANGE), envs["reference"], envs["safe"])
    checks = [v.check for v in results.values()]
    assert {c.contract_id: c.outcome for c in checks} == dict.fromkeys(
        (C1, C2, C3), CheckOutcome.PASS
    )
    assert derive_verdict(checks) is ReportVerdict.CHECKED
    assert github_check_conclusion(derive_verdict(checks)) is GitHubCheckConclusion.SUCCESS
    for verification in results.values():
        cited = set(verification.check.evidence_ids)
        assert cited == {e.evidence_id for e in verification.evidence}
        assert {e.environment.value for e in verification.evidence} == {"REFERENCE", "CANDIDATE"}


def test_join_change_is_blocked_for_the_right_reasons(envs: dict[str, str]) -> None:
    results = verify(contracts(JOIN_CHANGE), envs["reference"], envs["join"])
    checks = {cid: v.check for cid, v in results.items()}
    assert {cid: c.outcome for cid, c in checks.items()} == dict.fromkeys(
        (C1, C2, C3), CheckOutcome.FAIL
    )
    assert derive_verdict(list(checks.values())) is ReportVerdict.BLOCKED

    assert (
        Mismatch(
            key=(typed_value(1002),),
            column="amount",
            expected=typed_value(Decimal("49.99")),
            observed=typed_value(Decimal("99.98")),
        )
        in checks[C1].mismatch_examples
    )
    assert (
        Mismatch(
            key=(typed_value(1), typed_value("USD")),
            column="refund_total",
            expected=typed_value(Decimal("49.99")),
            observed=typed_value(Decimal("99.98")),
        )
        in checks[C2].mismatch_examples
    )

    fixture = build_fixture()
    items = Counter(i.order_id for i in fixture.items)
    refunded = {e.order_id for e in fixture.events if e.event_kind == "REFUND"}
    multi_item_events = sum(1 for e in fixture.events if items[e.order_id] > 1)
    assert checks[C1].mismatch_count == multi_item_events
    assert checks[C2].mismatch_count == sum(1 for o in refunded if items[o] > 1)
    assert checks[C3].mismatch_count == sum(1 for o in fixture.orders if items[o.order_id] > 1)
    for check in checks.values():
        assert check.expected_key_count == check.observed_key_count  # keys survive; values do not


def test_unapproved_bindings_run_nothing(envs: dict[str, str]) -> None:
    results = verify(contracts(SAFE_CHANGE, approve=False), envs["reference"], envs["safe"])
    for verification in results.values():
        assert verification.check.outcome is CheckOutcome.NOT_RUN
        assert verification.check.not_run_reason is NotRunReason.AWAITING_BINDING_REVIEW
        assert verification.evidence == ()  # no query ran on either side
    verdict = derive_verdict([v.check for v in results.values()])
    assert verdict is ReportVerdict.INCOMPLETE
    assert github_check_conclusion(verdict) is GitHubCheckConclusion.ACTION_REQUIRED


def test_rejected_bindings_are_not_run(envs: dict[str, str]) -> None:
    results = verify(contracts(SAFE_CHANGE, reject=True), envs["reference"], envs["safe"])
    assert {v.check.not_run_reason for v in results.values()} == {NotRunReason.BINDING_REJECTED}


def test_reference_violation_is_an_oracle_error_not_pr_corruption(
    envs: dict[str, str], new_database: DatabaseFactory
) -> None:
    reference = baseline(new_database)
    with psycopg.connect(reference) as connection:
        connection.execute("UPDATE legacy_payment_events SET amount = 500.00 WHERE event_id = 1002")
    results = verify(contracts(SAFE_CHANGE), reference, envs["safe"])
    c2 = results[C2]
    assert (c2.check.outcome, c2.check.error_code) == (
        CheckOutcome.ERROR,
        ErrorCode.ORACLE_VIOLATED_ON_REFERENCE,
    )
    assert [e.evidence_id for e in c2.evidence] == [
        f"{C2}:reference:precondition"
    ]  # candidate untouched


def test_binding_cannot_narrow_the_key_universe(envs: dict[str, str]) -> None:
    narrowed = override(
        C1,
        "SELECT source_event_id, order_id, entry_kind::text AS entry_kind, amount, occurred_at"
        " FROM ledger_entries WHERE order_id <> 1",
    )
    check = verify(contracts(SAFE_CHANGE, overrides=narrowed), envs["reference"], envs["safe"])[
        C1
    ].check
    assert check.outcome is CheckOutcome.FAIL
    assert [(m.key, m.observed) for m in check.mismatch_examples] == [
        ((typed_value(1001),), None),
        ((typed_value(1002),), None),
    ]


def test_unmapped_oracle_column_is_an_error(envs: dict[str, str]) -> None:
    gap = override(
        C1,
        "SELECT source_event_id, order_id, entry_kind::text AS entry_kind, amount"
        " FROM ledger_entries",
        occurred_at=None,
    )
    check = verify(contracts(SAFE_CHANGE, overrides=gap), envs["reference"], envs["safe"])[C1].check
    assert (check.outcome, check.error_code) == (CheckOutcome.ERROR, ErrorCode.BINDING_COVERAGE_GAP)


def test_duplicate_keys_are_an_error(envs: dict[str, str]) -> None:
    duplicated = override(
        C3,
        "SELECT order_id, currency::text AS currency, 0::numeric(18, 2) AS capture_total"
        " FROM orders UNION ALL SELECT order_id, currency::text, 0::numeric(18, 2) FROM orders",
    )
    check = verify(contracts(SAFE_CHANGE, overrides=duplicated), envs["reference"], envs["safe"])[
        C3
    ].check
    assert (check.outcome, check.error_code) == (CheckOutcome.ERROR, ErrorCode.DUPLICATE_KEY)


@pytest.mark.parametrize(
    "hostile_sql",
    [
        "SELECT source_event_id, order_id, entry_kind::text AS entry_kind, amount, occurred_at"
        " FROM ledger_entries; COMMIT; DELETE FROM ledger_entries",
        "WITH gone AS (DELETE FROM ledger_entries RETURNING *)"
        " SELECT source_event_id, order_id, entry_kind::text AS entry_kind, amount, occurred_at"
        " FROM gone",
    ],
    ids=["multi-statement", "writable-cte"],
)
def test_bindings_cannot_write(
    new_database: DatabaseFactory, envs: dict[str, str], hostile_sql: str
) -> None:
    target = candidate(new_database, SAFE_CHANGE)
    before = scalar(target, "SELECT count(*) FROM ledger_entries")
    check = verify(
        contracts(SAFE_CHANGE, overrides=override(C1, hostile_sql)), envs["reference"], target
    )[C1].check
    assert (check.outcome, check.error_code) == (CheckOutcome.ERROR, ErrorCode.QUERY_FAILED)
    assert scalar(target, "SELECT count(*) FROM ledger_entries") == before > 0


def test_statement_timeout_and_row_limit(envs: dict[str, str]) -> None:
    slow = override(
        C3,
        "SELECT order_id, currency::text AS currency, 0::numeric(18, 2) AS capture_total"
        " FROM orders, pg_sleep(1)",
    )
    timed_out = verify(
        contracts(SAFE_CHANGE, overrides=slow),
        envs["reference"],
        envs["safe"],
        QueryLimits(statement_timeout_ms=200),
    )[C3].check
    assert (timed_out.outcome, timed_out.error_code) == (CheckOutcome.ERROR, ErrorCode.QUERY_FAILED)

    capped = verify(
        contracts(SAFE_CHANGE), envs["reference"], envs["safe"], QueryLimits(max_rows=10)
    )
    assert {v.check.error_code for v in capped.values()} == {ErrorCode.RESULT_LIMIT_EXCEEDED}


def test_a_statement_that_ends_the_transaction_is_refused(envs: dict[str, str]) -> None:
    with (
        psycopg.connect(envs["safe"], autocommit=True) as connection,
        pytest.raises(QueryError) as caught,
    ):
        run_read_only(connection, "COMMIT")
    assert caught.value.code is ErrorCode.QUERY_FAILED
