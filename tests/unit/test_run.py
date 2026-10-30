import pytest
from pydantic import ValidationError

from conftest import ExampleLoader
from dataproof_contracts import NotRunReason, ReviewRun, RunInputs, RunState
from dataproof_contracts.enums import TERMINAL_RUN_STATES


def test_binding_review_is_a_check_reason_not_a_run_state() -> None:
    assert "AWAITING_BINDING_REVIEW" not in RunState.__members__
    assert NotRunReason.AWAITING_BINDING_REVIEW.value == "AWAITING_BINDING_REVIEW"


def test_terminal_states() -> None:
    assert {s.value for s in TERMINAL_RUN_STATES} == {"COMPLETE", "SUPERSEDED", "NOT_EXECUTED"}


def test_examples_are_valid(example: ExampleLoader) -> None:
    for name in ("webhook-complete", "binding-approval", "not-executed"):
        ReviewRun.model_validate(example(f"ReviewRun.{name}"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "example-org/other"),
        ("pull_request", 8),
        ("base_sha", "1" * 40),
        ("head_sha", "2" * 40),
        ("config_digest", "3" * 64),
        ("dataset_digest", "4" * 64),
        ("engine_version", "0.1.1"),
    ],
)
def test_every_input_changes_the_digest(example: ExampleLoader, field: str, value: object) -> None:
    inputs = RunInputs.model_validate(example("ReviewRun.webhook-complete")["inputs"])
    changed = RunInputs.model_validate({**inputs.model_dump(), field: value})
    assert changed.digest() != inputs.digest()
    assert RunInputs.model_validate(inputs.model_dump()).digest() == inputs.digest()


def test_input_digest_must_match_inputs(example: ExampleLoader) -> None:
    data = example("ReviewRun.webhook-complete")
    data["inputs"]["head_sha"] = "f" * 40
    with pytest.raises(ValidationError, match="input_digest"):
        ReviewRun.model_validate(data)


def test_approved_binding_run_needs_a_parent(example: ExampleLoader) -> None:
    data = example("ReviewRun.binding-approval")
    data["parent_run_id"] = None
    with pytest.raises(ValidationError, match="parent_run_id"):
        ReviewRun.model_validate(data)


def test_webhook_run_has_no_parent(example: ExampleLoader) -> None:
    data = example("ReviewRun.webhook-complete")
    data["parent_run_id"] = example("ReviewRun.binding-approval")["run_id"]
    with pytest.raises(ValidationError, match="parent_run_id"):
        ReviewRun.model_validate(data)


def test_run_cannot_be_its_own_parent(example: ExampleLoader) -> None:
    data = example("ReviewRun.binding-approval")
    data["parent_run_id"] = data["run_id"]
    with pytest.raises(ValidationError, match="own parent"):
        ReviewRun.model_validate(data)


def test_not_executed_requires_a_reason_and_only_then(example: ExampleLoader) -> None:
    missing = example("ReviewRun.not-executed")
    missing["not_executed_reason"] = None
    with pytest.raises(ValidationError, match="not_executed_reason"):
        ReviewRun.model_validate(missing)
    stray = example("ReviewRun.webhook-complete")
    stray["not_executed_reason"] = "FORK_PULL_REQUEST"
    with pytest.raises(ValidationError, match="not_executed_reason"):
        ReviewRun.model_validate(stray)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("inputs", "head_sha"), "abc1234"),
        (("created_at",), "2026-10-30T15:00:00"),
        (("state",), "AWAITING_BINDING_REVIEW"),
    ],
)
def test_rejects_ambiguous_inputs(
    example: ExampleLoader, path: tuple[str, ...], value: str
) -> None:
    data = example("ReviewRun.webhook-complete")
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ReviewRun.model_validate(data)


def test_lease_times_must_be_ordered(example: ExampleLoader) -> None:
    data = example("ReviewRun.webhook-complete")
    data["lease"] = {
        "worker_id": "worker-1",
        "fencing_token": 3,
        "acquired_at": "2026-10-30T15:00:00Z",
        "heartbeat_at": "2026-10-30T15:00:30Z",
        "expires_at": "2026-10-30T15:00:10Z",
    }
    with pytest.raises(ValidationError, match="lease times"):
        ReviewRun.model_validate(data)
