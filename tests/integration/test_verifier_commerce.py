"""The verifier against real PostgreSQL, using the commerce oracles and change-set bindings.

Read-only environments (reference, safe candidate, join candidate) are built once per module;
tests that change a database get their own.
"""

from collections import Counter
from collections.abc import Iterator
from decimal import Decimal

import psycopg
import pytest

from commerce_env import (
    C1,
    C2,
    C3,
    JOIN_CHANGE,
    SAFE_CHANGE,
    DatabaseFactory,
    baseline,
    candidate,
    contracts,
    database_factory,
    override,
    scalar,
    verify,
)
from commerce_scenario import build_fixture
from dataproof_contracts import (
    CheckOutcome,
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
    derive_verdict,
    run_read_only,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def envs(postgres_dsn: str) -> Iterator[dict[str, str]]:
    with database_factory(postgres_dsn) as create:
        yield {
            "reference": baseline(create),
            "safe": candidate(create, SAFE_CHANGE),
            "join": candidate(create, JOIN_CHANGE),
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


HOSTILE = pytest.mark.parametrize(
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


@HOSTILE
def test_hostile_bindings_are_stopped_before_execution(
    new_database: DatabaseFactory, envs: dict[str, str], hostile_sql: str
) -> None:
    target = candidate(new_database, SAFE_CHANGE)
    before = scalar(target, "SELECT count(*) FROM ledger_entries")
    contract_list = contracts(SAFE_CHANGE, overrides=override(C1, hostile_sql))
    verification = verify(contract_list, envs["reference"], target)[C1]
    assert verification.check.error_code is ErrorCode.BINDING_UNSUPPORTED_CONSTRUCT
    assert verification.binding_problem is not None
    assert verification.evidence == ()
    assert scalar(target, "SELECT count(*) FROM ledger_entries") == before > 0


@HOSTILE
def test_execution_layer_alone_also_refuses_writes(
    new_database: DatabaseFactory, hostile_sql: str
) -> None:
    """Defense in depth: even without binding review, execution cannot write."""
    target = candidate(new_database, SAFE_CHANGE)
    before = scalar(target, "SELECT count(*) FROM ledger_entries")
    with (
        psycopg.connect(target, autocommit=True) as connection,
        pytest.raises(QueryError) as caught,
    ):
        run_read_only(connection, hostile_sql)
    assert caught.value.code is ErrorCode.QUERY_FAILED
    assert scalar(target, "SELECT count(*) FROM ledger_entries") == before > 0


def test_statement_timeout_and_row_limit(envs: dict[str, str]) -> None:
    with (
        psycopg.connect(envs["safe"], autocommit=True) as connection,
        pytest.raises(QueryError) as caught,
    ):
        run_read_only(connection, "SELECT pg_sleep(1)", QueryLimits(statement_timeout_ms=200))
    assert caught.value.code is ErrorCode.QUERY_FAILED
    assert "statement timeout" in str(caught.value)

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
