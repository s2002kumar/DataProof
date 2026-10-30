from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from commerce_env import CHANGES, ORACLES, SAFE_CHANGE
from dataproof_contracts import (
    BindingReviewStatus,
    CheckOutcome,
    CheckResult,
    Comparator,
    ComparatorKind,
    ErrorCode,
    NotRunReason,
    ReportVerdict,
    typed_value,
)
from dataproof_verifier import (
    assemble_contract,
    compare_keyed,
    derive_verdict,
    load_binding,
    load_oracle,
    values_match,
)

EXACT = Comparator(kind=ComparatorKind.EXACT)
CENTS = Comparator(kind=ComparatorKind.DECIMAL, decimal_scale=2)
CENT_TOLERANT = Comparator(kind=ComparatorKind.DECIMAL, decimal_scale=2, tolerance=Decimal("0.01"))
INSTANT = Comparator(kind=ComparatorKind.INSTANT)
Rows = dict[tuple[object, ...], tuple[object, ...]]


@pytest.mark.parametrize(
    ("comparator", "expected", "observed", "match"),
    [
        (CENTS, Decimal("49.99"), Decimal("49.99"), True),
        (CENTS, Decimal("49.99"), Decimal("49.990"), True),
        (CENTS, Decimal("49.99"), Decimal("99.98"), False),
        (CENT_TOLERANT, Decimal("49.99"), Decimal("50.00"), True),
        (CENT_TOLERANT, Decimal("49.99"), Decimal("50.01"), False),
        (CENTS, None, None, True),
        (CENTS, None, Decimal(0), False),
        (CENTS, Decimal("49.99"), "49.99", False),
        (EXACT, "", None, False),
        (EXACT, "REFUND", "REFUND", True),
        (
            INSTANT,
            datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
            datetime(2026, 3, 8, 1, 30, tzinfo=timezone(timedelta(hours=-5))),
            True,
        ),
        (
            INSTANT,
            datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
            datetime(2026, 3, 8, 6, 30, tzinfo=timezone(timedelta(hours=-5))),
            False,
        ),
    ],
)
def test_values_match(
    comparator: Comparator, expected: object, observed: object, match: bool
) -> None:
    assert values_match(comparator, expected, observed) is match


def test_floats_are_never_compared() -> None:
    with pytest.raises(TypeError):
        values_match(CENTS, Decimal("49.99"), 49.99)


def test_missing_and_extra_keys_are_presence_mismatches() -> None:
    expected: Rows = {(1,): (Decimal("1.00"),), (2,): (Decimal("2.00"),)}
    observed: Rows = {(2,): (Decimal("2.00"),), (3,): (Decimal("3.00"),)}
    mismatches = compare_keyed(expected, observed, ["amount"], {"amount": CENTS})
    assert [(m.key, m.expected, m.observed) for m in mismatches] == [
        ((typed_value(1),), typed_value(1), None),
        ((typed_value(3),), None, typed_value(3)),
    ]


def test_keys_sort_naturally_and_composite_keys_stay_typed() -> None:
    expected: Rows = {(10, "USD"): (Decimal(1),), (9, "EUR"): (Decimal(1),)}
    observed: Rows = {(10, "USD"): (Decimal(2),), (9, "EUR"): (Decimal(2),)}
    keys = [m.key for m in compare_keyed(expected, observed, ["t"], {"t": CENTS})]
    assert keys == [(typed_value(9), typed_value("EUR")), (typed_value(10), typed_value("USD"))]


def _check(outcome: CheckOutcome, *, required: bool = True) -> CheckResult:
    return CheckResult(
        check_id=f"c-{outcome}-{required}",
        contract_id="c",
        contract_version=1,
        required=required,
        outcome=outcome,
        not_run_reason=NotRunReason.EXCLUDED if outcome is CheckOutcome.NOT_RUN else None,
        error_code=ErrorCode.QUERY_FAILED if outcome is CheckOutcome.ERROR else None,
        mismatch_count=1 if outcome is CheckOutcome.FAIL else 0,
        evidence_ids=("ev",) if outcome is CheckOutcome.FAIL else (),
        dataset_digest="0" * 64,
    )


PASS, FAIL, ERROR, NOT_RUN = (
    CheckOutcome.PASS,
    CheckOutcome.FAIL,
    CheckOutcome.ERROR,
    CheckOutcome.NOT_RUN,
)


@pytest.mark.parametrize(
    ("checks", "verdict"),
    [
        ([_check(PASS)], ReportVerdict.CHECKED),
        ([_check(PASS), _check(FAIL)], ReportVerdict.BLOCKED),
        ([_check(ERROR), _check(NOT_RUN)], ReportVerdict.BLOCKED),
        ([_check(PASS), _check(NOT_RUN)], ReportVerdict.INCOMPLETE),
        ([_check(PASS), _check(FAIL, required=False)], ReportVerdict.CHECKED_WITH_WARNINGS),
        ([_check(PASS), _check(NOT_RUN, required=False)], ReportVerdict.CHECKED_WITH_WARNINGS),
        ([], ReportVerdict.INCOMPLETE),
        ([_check(PASS, required=False)], ReportVerdict.INCOMPLETE),
    ],
)
def test_derive_verdict(checks: list[CheckResult], verdict: ReportVerdict) -> None:
    assert derive_verdict(checks) is verdict


def _load(name: str) -> tuple[Path, Path]:
    return ORACLES / name, CHANGES / SAFE_CHANGE / "bindings" / name


@pytest.mark.parametrize("name", sorted(p.name for p in ORACLES.glob("*.toml")))
def test_every_oracle_has_a_matching_binding(name: str) -> None:
    oracle_path, binding_path = _load(name)
    spec, oracle = load_oracle(oracle_path, source_path=name, base_sha="b" * 40)
    contract_id, binding = load_binding(binding_path, source_path=name, head_sha="c" * 40)
    assert contract_id == spec.contract_id
    contract = assemble_contract(spec, oracle, binding)
    assert contract.binding_review is BindingReviewStatus.PENDING
    assert contract.binding_coverage_gaps() == frozenset()


def test_binding_file_cannot_carry_oracle_policy(tmp_path: Path) -> None:
    _, binding_path = _load("c2_refund_totals.toml")
    smuggled = tmp_path / "binding.toml"
    smuggled.write_text(
        binding_path.read_text("utf-8").replace(
            "[column_mapping]", 'tolerance = "100.00"\n\n[column_mapping]'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_binding(smuggled, source_path="x", head_sha="c" * 40)


def test_approval_is_bound_to_the_exact_binding_digest() -> None:
    oracle_path, binding_path = _load("c1_event_preservation.toml")
    spec, oracle = load_oracle(oracle_path, source_path="o", base_sha="b" * 40)
    _, binding = load_binding(binding_path, source_path="b", head_sha="c" * 40)
    approved = assemble_contract(spec, oracle, binding, approved_digests={binding.digest()})
    assert approved.binding_review is BindingReviewStatus.APPROVED
    edited = binding.model_copy(update={"query_sql": f"{binding.query_sql} WHERE true"})
    stale = assemble_contract(spec, oracle, edited, approved_digests={binding.digest()})
    assert stale.binding_review is BindingReviewStatus.PENDING
    rejected = assemble_contract(spec, oracle, binding, rejected_digests={binding.digest()})
    assert rejected.binding_review is BindingReviewStatus.REJECTED
