"""Work package 3 exit evidence: every commerce change set gets its verdict for the right reason.

Faults: F1 (``-07``), F3 (``-03``), decimal loss (``-05``) and a timestamp shift (``-06``).
Clean controls: the safe backfill (``-01``), a restructure that retires the legacy table
(``-02``), and wider amount precision with normalized timestamps (``-04``). Expected outcomes
live here, never in the change sets themselves (BENCHMARK leakage rules).
"""

from collections.abc import Iterator
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from commerce_env import C1, C2, C3, baseline, candidate, contracts, database_factory, verify
from commerce_scenario import build_fixture
from dataproof_contracts import (
    CheckOutcome,
    DecimalValue,
    Mismatch,
    RelationProvenance,
    ReportVerdict,
    TimestampValue,
    typed_value,
)
from dataproof_verifier import Verification, derive_verdict

pytestmark = pytest.mark.integration

PASS, FAIL = CheckOutcome.PASS, CheckOutcome.FAIL
EXPECTED: dict[str, tuple[CheckOutcome, CheckOutcome, CheckOutcome]] = {
    "ledger-backfill-01": (PASS, PASS, PASS),
    "ledger-backfill-02": (PASS, PASS, PASS),
    "ledger-backfill-03": (FAIL, PASS, PASS),
    "ledger-backfill-04": (PASS, PASS, PASS),
    "ledger-backfill-05": (FAIL, FAIL, FAIL),
    "ledger-backfill-06": (FAIL, PASS, PASS),
    "ledger-backfill-07": (FAIL, FAIL, FAIL),
}
Runs = dict[str, dict[str, Verification]]


@pytest.fixture(scope="module")
def runs(postgres_dsn: str) -> Iterator[Runs]:
    with database_factory(postgres_dsn) as create:
        reference = baseline(create)
        yield {
            change: verify(contracts(change), reference, candidate(create, change))
            for change in EXPECTED
        }


def examples(verification: Verification) -> tuple[Mismatch, ...]:
    """The bounded mismatch examples (the full count is ``mismatch_count``)."""
    assert verification.check.mismatch_examples
    return verification.check.mismatch_examples


@pytest.mark.parametrize("change", sorted(EXPECTED))
def test_each_change_set_gets_its_expected_outcomes(runs: Runs, change: str) -> None:
    results = runs[change]
    outcomes = tuple(results[contract].check.outcome for contract in (C1, C2, C3))
    assert outcomes == EXPECTED[change]
    verdict = derive_verdict([v.check for v in results.values()])
    expected_verdict = ReportVerdict.CHECKED if FAIL not in outcomes else ReportVerdict.BLOCKED
    assert verdict is expected_verdict


def test_clean_controls_have_no_false_positives(runs: Runs) -> None:
    for change in ("ledger-backfill-01", "ledger-backfill-02", "ledger-backfill-04"):
        for verification in runs[change].values():
            assert verification.check.mismatch_count == 0, (change, verification.check.contract_id)
    # The restructure retired the legacy table; its bindings never read it.
    analysis = runs["ledger-backfill-02"][C1].binding_analysis
    assert analysis is not None
    assert [(r.name, r.provenance) for r in analysis.relations] == [
        ("public.ledger_entries", RelationProvenance.CREATED)
    ]


def test_f3_swaps_exactly_the_tied_refunds_and_aggregates_see_nothing(runs: Runs) -> None:
    f3 = runs["ledger-backfill-03"]
    assert f3[C1].check.mismatch_examples == (
        Mismatch(
            key=(typed_value(1004),),
            column="amount",
            expected=typed_value(Decimal("12.50")),
            observed=typed_value(Decimal("37.49")),
        ),
        Mismatch(
            key=(typed_value(1005),),
            column="amount",
            expected=typed_value(Decimal("37.49")),
            observed=typed_value(Decimal("12.50")),
        ),
    )
    # Per-order refund and capture totals are unchanged, so aggregate reconciliation passes.
    assert (f3[C2].check.outcome, f3[C3].check.outcome) == (PASS, PASS)


def test_decimal_loss_rounds_every_fractional_amount(runs: Runs) -> None:
    c1 = runs["ledger-backfill-05"][C1].check
    fractional = [e for e in build_fixture().events if e.amount != e.amount.to_integral_value()]
    assert c1.mismatch_count == len(fractional)
    for mismatch in examples(runs["ledger-backfill-05"][C1]):
        assert mismatch.column == "amount"
        assert isinstance(mismatch.expected, DecimalValue)
        assert isinstance(mismatch.observed, DecimalValue)
        rounded = mismatch.expected.value.quantize(Decimal(1), ROUND_HALF_UP)
        assert mismatch.observed.value == rounded


def test_timestamp_shift_moves_every_event_by_the_local_offset(runs: Runs) -> None:
    c1 = runs["ledger-backfill-06"][C1].check
    assert c1.mismatch_count == len(build_fixture().events)
    shifts = set()
    for mismatch in examples(runs["ledger-backfill-06"][C1]):
        assert mismatch.column == "occurred_at"
        assert isinstance(mismatch.expected, TimestampValue)
        assert isinstance(mismatch.observed, TimestampValue)
        shifts.add(mismatch.observed.value - mismatch.expected.value)
    assert shifts <= {timedelta(hours=4), timedelta(hours=5)}
    # Totals do not depend on timestamps, so C2 and C3 pass: only keyed comparison sees this.
    assert runs["ledger-backfill-06"][C2].check.outcome is PASS
