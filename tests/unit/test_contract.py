import pytest
from pydantic import ValidationError

from conftest import ExampleLoader
from dataproof_contracts import CandidateBinding, Comparator, DataContract

PENDING = "DataContract.c1-pending-binding"
APPROVED = "DataContract.c1-approved-binding"


def test_examples_are_valid(example: ExampleLoader) -> None:
    pending = DataContract.model_validate(example(PENDING))
    approved = DataContract.model_validate(example(APPROVED))
    assert pending.binding_coverage_gaps() == frozenset()
    assert approved.candidate_binding is not None
    assert approved.approved_binding_digest == approved.candidate_binding.digest()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tolerance", "0.01"),
        ("required", False),
        ("key_columns", ["order_id"]),
        ("expected_columns", {"amount": {"kind": "EXACT"}}),
    ],
)
def test_binding_cannot_carry_oracle_policy(
    example: ExampleLoader, field: str, value: object
) -> None:
    binding = example(PENDING)["candidate_binding"]
    binding[field] = value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CandidateBinding.model_validate(binding)


def test_changed_binding_invalidates_an_approval(example: ExampleLoader) -> None:
    data = example(APPROVED)
    data["candidate_binding"]["query_sql"] += " WHERE amount > 0"
    with pytest.raises(ValidationError, match="needs new review"):
        DataContract.model_validate(data)


def test_approval_requires_digest_and_pending_forbids_it(example: ExampleLoader) -> None:
    approved = example(APPROVED)
    approved["approved_binding_digest"] = None
    with pytest.raises(ValidationError, match="approved_binding_digest"):
        DataContract.model_validate(approved)
    pending = example(PENDING)
    pending["approved_binding_digest"] = example(APPROVED)["approved_binding_digest"]
    with pytest.raises(ValidationError, match="approved_binding_digest"):
        DataContract.model_validate(pending)


def test_binding_always_needs_review_and_absence_needs_none(example: ExampleLoader) -> None:
    unreviewed = example(PENDING)
    unreviewed["binding_review"] = "NOT_REQUIRED"
    with pytest.raises(ValidationError, match="always requires review"):
        DataContract.model_validate(unreviewed)
    no_binding = example(PENDING)
    no_binding["candidate_binding"] = None
    with pytest.raises(ValidationError, match="NOT_REQUIRED"):
        DataContract.model_validate(no_binding)


def test_unmapped_oracle_columns_are_reported_not_dropped(example: ExampleLoader) -> None:
    data = example(PENDING)
    del data["candidate_binding"]["column_mapping"]["amount"]
    del data["candidate_binding"]["column_mapping"]["event_id"]
    assert DataContract.model_validate(data).binding_coverage_gaps() == {"amount", "event_id"}


def test_binding_needs_a_query_or_route(example: ExampleLoader) -> None:
    binding = example(PENDING)["candidate_binding"]
    binding["query_sql"] = None
    with pytest.raises(ValidationError, match="query_sql, api_route"):
        CandidateBinding.model_validate(binding)


@pytest.mark.parametrize(
    "comparator",
    [
        {"kind": "DECIMAL"},
        {"kind": "EXACT", "decimal_scale": 2},
        {"kind": "INSTANT", "tolerance": "1"},
        {"kind": "DECIMAL", "decimal_scale": 2, "tolerance": "-0.01"},
        {"kind": "DECIMAL", "decimal_scale": 2, "tolerance": 0.01},
    ],
)
def test_comparator_rules(comparator: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Comparator.model_validate(comparator)


def test_oracle_column_cannot_be_both_key_and_value(example: ExampleLoader) -> None:
    data = example(PENDING)
    data["oracle"]["key_columns"] = ["event_id", "amount"]
    with pytest.raises(ValidationError, match="both key and compared value"):
        DataContract.model_validate(data)
