from typing import Any

import pytest
from pydantic import ValidationError

from conftest import ExampleLoader
from dataproof_contracts import Investigation, InvestigationStatus


def test_example_is_valid(example: ExampleLoader) -> None:
    investigation = Investigation.model_validate(example("Investigation.f1-diagnosis"))
    assert investigation.status is InvestigationStatus.COMPLETED


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"hypotheses": []}, "needs hypotheses and a caveat"),
        ({"caveat": None}, "needs hypotheses and a caveat"),
        ({"failure": "model declined"}, "failure is required exactly"),
        ({"status": "NOT_AVAILABLE", "failure": "declined"}, "offers no conclusions"),
        ({"status": "INVALID_OUTPUT"}, "failure is required exactly"),
    ],
)
def test_conclusions_belong_only_to_completed_investigations(
    example: ExampleLoader, change: dict[str, Any], message: str
) -> None:
    data = {**example("Investigation.f1-diagnosis"), **change}
    with pytest.raises(ValidationError, match=message):
        Investigation.model_validate(data)


def test_hypotheses_cannot_cite_tool_results_that_do_not_exist(example: ExampleLoader) -> None:
    data = example("Investigation.f1-diagnosis")
    data["hypotheses"][0]["cited_evidence_ids"] = ["investigation.tool.9"]
    with pytest.raises(ValidationError, match="cites a tool result"):
        Investigation.model_validate(data)


def test_every_hypothesis_cites_something(example: ExampleLoader) -> None:
    data = example("Investigation.f1-diagnosis")
    data["hypotheses"][0]["cited_evidence_ids"] = []
    with pytest.raises(ValidationError):
        Investigation.model_validate(data)


@pytest.mark.parametrize(
    "call",
    [
        {"outcome": "DENIED"},  # a refused call cannot carry evidence
        {"evidence_id": "investigation.tool.2"},  # the ID is fixed by the sequence
        {"evidence_id": None},  # a successful call always yields evidence
    ],
)
def test_tool_call_evidence_is_fixed_by_its_outcome_and_sequence(
    example: ExampleLoader, call: dict[str, Any]
) -> None:
    data = example("Investigation.f1-diagnosis")
    data["tool_calls"][0].update(call)
    with pytest.raises(ValidationError):
        Investigation.model_validate(data)


def test_tool_calls_are_numbered_in_order(example: ExampleLoader) -> None:
    data = example("Investigation.f1-diagnosis")
    data["tool_calls"][0]["sequence"] = 2
    data["tool_calls"][0]["evidence_id"] = "investigation.tool.2"
    data["hypotheses"][0]["cited_evidence_ids"] = ["investigation.tool.2"]
    with pytest.raises(ValidationError, match="numbered 1, 2"):
        Investigation.model_validate(data)
