"""Every change set of the subscriptions example at the database tier.

Each change set is migrated into a fresh candidate from the same reference (base migration plus
the fixture), its bindings are approved, and the three oracles are verified exactly as the Action
verifies them, without Docker. Faults must fail the contract that observes them, for the rows
and columns the fixture makes deterministic; clean controls must pass every contract.
"""

import os
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from commerce_env import DatabaseFactory
from dataproof_contracts import (
    BindingReviewStatus,
    CheckOutcome,
    CheckResult,
    DataContract,
    DecimalValue,
    ErrorCode,
    NotRunReason,
    NullValue,
    TextValue,
)
from dataproof_verifier import (
    QueryLimits,
    Verification,
    assemble_contract,
    load_binding,
    load_oracle,
    type_check,
    verify_contract,
)
from subscriptions_env import CHANGES, EXAMPLE, INVOICE_AMOUNTS

pytestmark = pytest.mark.integration

ORACLES = EXAMPLE / "dataproof" / "oracles"
FIXTURE = EXAMPLE / "dataproof" / "fixture.sql"
BILLING, STATE = "subscriptions.billing_periods", "subscriptions.subscription_state"
BASE_SHA, HEAD_SHA = "b" * 40, "c" * 40
PASS, FAIL = CheckOutcome.PASS, CheckOutcome.FAIL


def migrate(dsn: str, change: str | None) -> None:
    config = Config()
    config.set_main_option("script_location", str(EXAMPLE / "migrations"))
    config.set_main_option("path_separator", "os")
    locations = [EXAMPLE / "migrations" / "versions"]
    if change is not None:
        locations.append(CHANGES / change)
    config.set_main_option("version_locations", os.pathsep.join(map(str, locations)))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def baseline(new_database: DatabaseFactory) -> str:
    dsn = new_database()
    migrate(dsn, None)
    with psycopg.connect(dsn) as connection:
        connection.execute(FIXTURE.read_text("utf-8"))
        connection.commit()
    return dsn


def contracts(change: str) -> list[DataContract]:
    result = []
    for oracle_path in sorted(ORACLES.glob("*.toml")):
        spec, oracle = load_oracle(oracle_path, source_path=oracle_path.name, base_sha=BASE_SHA)
        binding_path = CHANGES / change / "bindings" / oracle_path.name
        binding = None
        approved: set[str] = set()
        if binding_path.is_file():
            _, binding = load_binding(
                binding_path, source_path=binding_path.name, head_sha=HEAD_SHA
            )
            approved = {binding.digest()}
        result.append(assemble_contract(spec, oracle, binding, approved_digests=approved))
    return result


@pytest.fixture(scope="module")
def reference(postgres_dsn: str) -> Iterator[str]:
    from commerce_env import database_factory

    with database_factory(postgres_dsn) as create:
        yield baseline(create)


TYPE_CHECKS: dict[str, dict[str, CheckResult]] = {}
"""The C8 type check of every contract, by change set, kept beside the data verifications."""


def verify(change: str, reference: str, new_database: DatabaseFactory) -> dict[str, Verification]:
    candidate = baseline(new_database)
    migrate(candidate, change)
    with (
        psycopg.connect(reference, autocommit=True) as ref,
        psycopg.connect(candidate, autocommit=True) as cand,
    ):
        results = {
            c.contract_id: verify_contract(c, ref, cand, "0" * 64, QueryLimits())
            for c in contracts(change)
        }
    TYPE_CHECKS[change] = {
        c.contract_id: type_check(c, results[c.contract_id]) for c in contracts(change)
    }
    return results


def scalar(dsn: str, query: str) -> Any:
    with psycopg.connect(dsn) as connection:
        (row,) = connection.execute(query).fetchall()
        return row[0]


def columns(verification: Verification) -> set[str | None]:
    return {m.column for m in verification.check.mismatch_examples}


@pytest.mark.parametrize("change", ["money-columns", "invoice-index-and-comment", "plan-catalog"])
def test_clean_controls_pass_every_contract(
    change: str, reference: str, new_database: DatabaseFactory
) -> None:
    results = verify(change, reference, new_database)
    assert {k: v.check.outcome for k, v in results.items()} == {
        INVOICE_AMOUNTS: PASS,
        BILLING: PASS,
        STATE: PASS,
    }, {k: (v.check.outcome, v.check.error_code) for k, v in results.items()}


def _only(results: dict[str, Verification], failing: set[str]) -> None:
    for contract_id, verification in results.items():
        expected = FAIL if contract_id in failing else PASS
        assert verification.check.outcome is expected, (
            contract_id,
            verification.check.outcome,
            verification.check.error_code,
        )


@pytest.mark.parametrize("change", ["money-columns", "invoice-index-and-comment", "plan-catalog"])
def test_clean_controls_pass_the_type_check_too(
    change: str, reference: str, new_database: DatabaseFactory
) -> None:
    verify(change, reference, new_database)
    assert {k: v.outcome for k, v in TYPE_CHECKS[change].items()} == {
        INVOICE_AMOUNTS: PASS,
        BILLING: PASS,
        STATE: PASS,
    }


def test_money_declared_without_cents_fails_the_type_check(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Authored (C8): numeric(12, 0) cannot hold cents whatever the fixture holds."""
    results = verify("money-columns-whole-units", reference, new_database)
    _only(results, {INVOICE_AMOUNTS})
    types = TYPE_CHECKS["money-columns-whole-units"][INVOICE_AMOUNTS]
    assert types.outcome is FAIL
    assert {(m.key[0].value, m.observed.value) for m in types.mismatch_examples} == {  # type: ignore[union-attr]
        ("subtotal", "numeric(12, 0)"),
        ("tax", "numeric(12, 0)"),
        ("total", "numeric(12, 0)"),
    }
    assert types.evidence_ids == (f"{INVOICE_AMOUNTS}:candidate:binding",)


def test_a_naive_timestamp_column_has_no_instant(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Authored (C8): timestamp without time zone. The values cannot even be compared, which
    the data check reports as an error; the type check names the column and the type."""
    results = verify("period-naive-timestamp", reference, new_database)
    billing = results[BILLING].check
    assert billing.outcome is CheckOutcome.ERROR
    assert billing.error_code is ErrorCode.UNSUPPORTED_VALUE_TYPE
    types = TYPE_CHECKS["period-naive-timestamp"][BILLING]
    assert types.outcome is FAIL
    assert {(m.key[0].value, m.observed.value) for m in types.mismatch_examples} == {  # type: ignore[union-attr]
        ("period_start", "timestamp without time zone"),
        ("period_end", "timestamp without time zone"),
    }
    for contract_id in (INVOICE_AMOUNTS, STATE):
        assert results[contract_id].check.outcome is PASS
        assert TYPE_CHECKS["period-naive-timestamp"][contract_id].outcome is PASS


def test_the_type_check_waits_for_the_data_check(
    reference: str, new_database: DatabaseFactory
) -> None:
    """An unapproved binding runs no query, so there are no types to judge: NOT_RUN."""
    candidate = baseline(new_database)
    migrate(candidate, "money-columns")
    (contract,) = [c for c in contracts("money-columns") if c.contract_id == INVOICE_AMOUNTS]
    pending = contract.model_copy(
        update={"binding_review": BindingReviewStatus.PENDING, "approved_binding_digest": None}
    )
    with (
        psycopg.connect(reference, autocommit=True) as ref,
        psycopg.connect(candidate, autocommit=True) as cand,
    ):
        verification = verify_contract(pending, ref, cand, "0" * 64, QueryLimits())
    assert verification.check.not_run_reason is NotRunReason.AWAITING_BINDING_REVIEW
    assert verification.candidate_types is None
    types = type_check(pending, verification)
    assert (types.outcome, types.not_run_reason) == (
        CheckOutcome.NOT_RUN,
        NotRunReason.AWAITING_BINDING_REVIEW,
    ), "the type check waits for the same review"


def test_integer_division_floors_most_invoices(
    reference: str, new_database: DatabaseFactory
) -> None:
    results = verify("money-columns-truncated", reference, new_database)
    _only(results, {INVOICE_AMOUNTS})
    amounts = results[INVOICE_AMOUNTS].check
    with_cents = scalar(
        reference,
        "SELECT count(*) FILTER (WHERE subtotal_cents % 100 <> 0) "
        "+ count(*) FILTER (WHERE tax_cents % 100 <> 0) "
        "+ count(*) FILTER (WHERE total_cents % 100 <> 0) FROM invoices",
    )
    assert amounts.mismatch_count == with_cents > 100


def test_a_float_round_trip_loses_a_cent_on_exactly_the_predicted_totals(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Sourced: a money conversion through binary floating point that truncates."""
    results = verify("money-columns-float-roundtrip", reference, new_database)
    _only(results, {INVOICE_AMOUNTS})
    amounts = results[INVOICE_AMOUNTS].check
    with psycopg.connect(reference) as connection:
        totals = [int(r[0]) for r in connection.execute("SELECT total_cents FROM invoices")]
    predicted = sum(1 for t in totals if int((t / 100) * 100) != t)
    assert predicted >= 10
    assert amounts.mismatch_count == predicted
    assert columns(results[INVOICE_AMOUNTS]) == {"total"}
    example = amounts.mismatch_examples[0]
    assert isinstance(example.expected, DecimalValue)
    assert isinstance(example.observed, DecimalValue)
    assert example.expected.value - example.observed.value == Decimal("0.01")


def test_a_session_time_zone_shifts_every_period(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Sourced: instants rewritten through a session whose zone is not UTC."""
    results = verify("period-session-timezone", reference, new_database)
    _only(results, {BILLING})
    billing = results[BILLING].check
    invoices = scalar(reference, "SELECT count(*) FROM invoices")
    assert billing.mismatch_count == 2 * invoices == 374
    assert columns(results[BILLING]) <= {"period_start", "period_end"}


def test_a_join_that_fans_out_doubles_tax(reference: str, new_database: DatabaseFactory) -> None:
    """Sourced: an insert computed through a join with two matching rate rows per invoice."""
    results = verify("tax-rates-fanout", reference, new_database)
    _only(results, {INVOICE_AMOUNTS})
    amounts = results[INVOICE_AMOUNTS].check
    taxable = scalar(reference, "SELECT count(*) FROM invoices WHERE subtotal_cents >= 8")
    assert amounts.mismatch_count >= taxable
    assert columns(results[INVOICE_AMOUNTS]) <= {"tax", "total"}


def test_a_batch_window_that_excludes_its_start_skips_the_first_invoice(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Sourced: batched backfills whose window arithmetic silently leaves rows untouched."""
    results = verify("backfill-batches-skip-first", reference, new_database)
    _only(results, {INVOICE_AMOUNTS})
    amounts = results[INVOICE_AMOUNTS].check
    first = scalar(reference, "SELECT min(invoice_id) FROM invoices")
    assert amounts.mismatch_count == 3
    assert {m.key[0].value for m in amounts.mismatch_examples} == {first}  # type: ignore[union-attr]
    assert all(
        m.observed is not None and m.observed.type == "null" for m in amounts.mismatch_examples
    )


def test_a_sentinel_date_is_not_no_date(reference: str, new_database: DatabaseFactory) -> None:
    """Sourced: unpaid records acquire a zero or epoch date in place of NULL."""
    results = verify("paid-at-sentinel", reference, new_database)
    _only(results, {BILLING})
    billing = results[BILLING].check
    unpaid = scalar(reference, "SELECT count(*) FROM invoices WHERE paid_at IS NULL")
    assert billing.mismatch_count == unpaid == 38
    assert columns(results[BILLING]) == {"paid_at"}


def test_a_split_that_drops_a_plan_orphans_its_subscriptions(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Sourced: a table split that leaves rows pointing at nothing."""
    results = verify("plan-catalog-orphans", reference, new_database)
    _only(results, {STATE})
    state = results[STATE].check
    on_free = scalar(
        reference,
        "SELECT count(*) FROM subscriptions AS s JOIN plans AS p ON p.plan_id = s.plan_id "
        "WHERE p.price_cents = 0",
    )
    assert state.mismatch_count == on_free > 0
    assert columns(results[STATE]) == {"plan_code"}
    for mismatch in state.mismatch_examples:
        assert isinstance(mismatch.expected, TextValue)
        assert mismatch.expected.value == "free"
        assert isinstance(mismatch.observed, NullValue)


def test_a_positional_copy_swaps_subtotal_and_tax(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Sourced: INSERT ... SELECT * into a table whose columns are in another order."""
    results = verify("invoices-copied-positionally", reference, new_database)
    _only(results, {INVOICE_AMOUNTS})
    amounts = results[INVOICE_AMOUNTS].check
    differing = scalar(reference, "SELECT count(*) FROM invoices WHERE subtotal_cents <> tax_cents")
    assert amounts.mismatch_count == 2 * differing == 302
    assert columns(results[INVOICE_AMOUNTS]) == {"subtotal", "tax"}


def test_an_import_whose_guard_misses_null_keys_duplicates_them(
    reference: str, new_database: DatabaseFactory
) -> None:
    """Sourced: a backfill meant to run once, run twice, with a guard NULL never satisfies."""
    results = verify("import-not-idempotent", reference, new_database)
    _only(results, {INVOICE_AMOUNTS, BILLING})
    unreferenced = scalar(reference, "SELECT count(*) FROM invoices WHERE external_ref IS NULL")
    for contract_id in (INVOICE_AMOUNTS, BILLING):
        check = results[contract_id].check
        assert check.mismatch_count == 2 * unreferenced > 0
        assert all(m.expected is None for m in check.mismatch_examples), "extra keys only"
        assert check.expected_key_count is not None
        assert check.observed_key_count == check.expected_key_count + 2 * unreferenced


def test_every_change_set_is_covered_here() -> None:
    covered = {
        "money-columns",
        "invoice-index-and-comment",
        "plan-catalog",
        "money-columns-truncated",
        "money-columns-float-roundtrip",
        "period-session-timezone",
        "tax-rates-fanout",
        "backfill-batches-skip-first",
        "paid-at-sentinel",
        "plan-catalog-orphans",
        "invoices-copied-positionally",
        "import-not-idempotent",
        "money-columns-whole-units",
        "period-naive-timestamp",
    }
    assert {p.name for p in CHANGES.iterdir() if p.is_dir()} == covered


def test_the_fixture_is_the_committed_one() -> None:
    """The generator reproduces the committed fixture exactly."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_fixture", EXAMPLE / "scripts" / "make_fixture.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build() == Path(FIXTURE).read_text("utf-8")
