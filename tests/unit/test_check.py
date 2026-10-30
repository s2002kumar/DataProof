from decimal import Decimal

import pytest
from pydantic import ValidationError

from conftest import ExampleLoader
from dataproof_contracts import CheckOutcome, CheckResult, DecimalValue, Mismatch, typed_value

F1 = "CheckResult.f1-detected"


def test_f1_example_records_the_multiplied_refund(example: ExampleLoader) -> None:
    result = CheckResult.model_validate(example(F1))
    assert result.outcome is CheckOutcome.FAIL
    (mismatch,) = result.mismatch_examples
    assert mismatch.key == (typed_value(1002),)
    assert mismatch.expected == DecimalValue(value=Decimal("49.99"))
    assert mismatch.observed == DecimalValue(value=Decimal("99.98"))


def test_other_examples_are_valid(example: ExampleLoader) -> None:
    CheckResult.model_validate(example("CheckResult.safe-pass"))
    CheckResult.model_validate(example("CheckResult.awaiting-binding-review"))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"outcome": "NOT_RUN"}, "not_run_reason"),
        ({"not_run_reason": "EXCLUDED"}, "not_run_reason"),
        ({"outcome": "ERROR"}, "error_code"),
        ({"error_code": "CANDIDATE_BUILD_FAILED"}, "error_code"),
        ({"outcome": "PASS"}, "PASS cannot report mismatches"),
        ({"evidence_ids": []}, "must cite deterministic evidence"),
        ({"mismatch_count": 0}, "more mismatch examples"),
    ],
)
def test_outcome_must_be_supported_by_fields(
    example: ExampleLoader, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CheckResult.model_validate({**example(F1), **changes})


def test_not_run_cannot_carry_evidence(example: ExampleLoader) -> None:
    data = example("CheckResult.awaiting-binding-review")
    data["evidence_ids"] = ["ev-c1-amount-query"]
    with pytest.raises(ValidationError, match="did not run"):
        CheckResult.model_validate(data)


def test_mismatch_distinguishes_absent_from_null() -> None:
    missing_event = Mismatch(key=(typed_value(1002),), expected=typed_value(None), observed=None)
    assert missing_event.observed is None
    assert missing_event.expected == typed_value(None)


@pytest.mark.parametrize(
    ("expected", "observed"),
    [(None, None), (Decimal("49.99"), Decimal("49.99"))],
)
def test_mismatch_must_be_a_difference(expected: Decimal | None, observed: Decimal | None) -> None:
    with pytest.raises(ValidationError):
        Mismatch(
            key=(typed_value(1002),),
            expected=None if expected is None else typed_value(expected),
            observed=None if observed is None else typed_value(observed),
        )


def test_composite_keys_are_typed_tuples() -> None:
    mismatch = Mismatch(
        key=(typed_value(300), typed_value("USD")),
        column="refund_total",
        expected=typed_value(Decimal("49.99")),
        observed=typed_value(Decimal("49.98")),
    )
    assert [v.type for v in mismatch.key] == ["integer", "text"]
