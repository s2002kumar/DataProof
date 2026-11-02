from typing import Any

import pytest
from pydantic import ValidationError

from conftest import ExampleLoader
from dataproof_contracts import Reproduction, ReproductionStatus


def test_example_is_valid(example: ExampleLoader) -> None:
    reproduction = Reproduction.model_validate(example("Reproduction.f1-refund"))
    assert reproduction.status is ReproductionStatus.REPRODUCED


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"reason": "it did not fail"}, "reason is required exactly"),
        ({"status": "NOT_REPRODUCED"}, "reason is required exactly"),
        ({"result": None}, "needs the failing result"),
        ({"subset_sha256": None}, "needs the subset and its digest"),
        ({"subset": []}, "needs the subset and its digest"),
        ({"signature": []}, "at least 1"),
    ],
)
def test_a_reproduction_claims_only_what_it_shows(
    example: ExampleLoader, change: dict[str, Any], message: str
) -> None:
    data = {**example("Reproduction.f1-refund"), **change}
    with pytest.raises(ValidationError, match=message):
        Reproduction.model_validate(data)


def test_a_passing_result_cannot_be_called_reproduced(example: ExampleLoader) -> None:
    data = example("Reproduction.f1-refund")
    data["result"].update(outcome="PASS", mismatch_count=0, mismatch_examples=[], evidence_ids=[])
    with pytest.raises(ValidationError, match="needs the failing result"):
        Reproduction.model_validate(data)


def test_not_reproduced_with_a_reason_is_valid(example: ExampleLoader) -> None:
    data = example("Reproduction.f1-refund")
    data.update(status="NOT_REPRODUCED", reason="the check is PASS on the subset")
    data["result"].update(outcome="PASS", mismatch_count=0, mismatch_examples=[])
    assert Reproduction.model_validate(data).reason == "the check is PASS on the subset"


def test_the_result_must_belong_to_the_reproduced_check(example: ExampleLoader) -> None:
    data = example("Reproduction.f1-refund")
    data["result"]["check_id"] = "commerce.c2_refund_totals"
    with pytest.raises(ValidationError, match="another check"):
        Reproduction.model_validate(data)
