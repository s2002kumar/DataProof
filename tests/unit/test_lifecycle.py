from datetime import timedelta
from itertools import pairwise

import pytest

from dataproof_contracts import RunState
from dataproof_contracts.enums import TERMINAL_RUN_STATES
from dataproof_control import (
    CANCELLABLE_STATES,
    INTERRUPTED_STATES,
    TRANSITIONS,
    WORKING_STATES,
    IllegalTransitionError,
    RetryPolicy,
    check_transition,
)

S = RunState
HAPPY_PATH = [
    S.QUEUED,
    S.FETCHING,
    S.ANALYZING,
    S.PROVISIONING,
    S.MIGRATING,
    S.VERIFYING,
    S.INVESTIGATING,
    S.REPRODUCING,
    S.REPORTING,
    S.READY_FOR_REVIEW,
    S.CLEANING,
    S.COMPLETE,
]


def test_every_state_has_an_entry() -> None:
    assert set(TRANSITIONS) == set(RunState)


def test_happy_path_is_legal_including_optional_investigation() -> None:
    for current, target in pairwise(HAPPY_PATH):
        check_transition(current, target)
    check_transition(S.VERIFYING, S.REPORTING)
    check_transition(S.INVESTIGATING, S.REPORTING)


def test_terminal_states_have_no_way_out() -> None:
    for state in TERMINAL_RUN_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_every_state_can_reach_a_terminal_state() -> None:
    for start in RunState:
        seen, frontier = {start}, [start]
        while frontier:
            for target in TRANSITIONS[frontier.pop()]:
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        assert seen & TERMINAL_RUN_STATES, start


def test_interruptions_lead_only_to_cleanup() -> None:
    for state in INTERRUPTED_STATES:
        assert TRANSITIONS[state] == {S.CLEANING}
    for state in WORKING_STATES:
        assert TRANSITIONS[state] >= INTERRUPTED_STATES


def test_cleanup_ends_in_complete_or_superseded_only() -> None:
    assert TRANSITIONS[S.CLEANING] == {S.COMPLETE, S.SUPERSEDED}
    entries = {state for state, targets in TRANSITIONS.items() if S.CLEANING in targets}
    assert entries == INTERRUPTED_STATES | {S.READY_FOR_REVIEW}


def test_only_cleanup_reaches_complete_and_superseded() -> None:
    for state, targets in TRANSITIONS.items():
        if S.COMPLETE in targets or S.SUPERSEDED in targets:
            assert state is S.CLEANING


def test_not_executed_is_decided_by_analysis_before_anything_is_provisioned() -> None:
    sources = {state for state, targets in TRANSITIONS.items() if S.NOT_EXECUTED in targets}
    assert sources == {S.ANALYZING}


def test_review_can_be_cancelled_but_not_timed_out_or_errored() -> None:
    assert TRANSITIONS[S.READY_FOR_REVIEW] == {S.CLEANING, S.CANCELLED}
    assert S.READY_FOR_REVIEW in CANCELLABLE_STATES


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.MIGRATING, S.PROVISIONING),  # no going back: a retry repeats a stage in place
        (S.QUEUED, S.MIGRATING),  # no skipping stages
        (S.COMPLETE, S.QUEUED),
        (S.ERROR, S.REPORTING),  # cleanup never rewrites outcomes into a report
        (S.CLEANING, S.ERROR),
        (S.READY_FOR_REVIEW, S.COMPLETE),  # previews are always torn down
        (S.VERIFYING, S.NOT_EXECUTED),
    ],
)
def test_illegal_transitions_are_rejected(current: RunState, target: RunState) -> None:
    with pytest.raises(IllegalTransitionError, match="illegal run transition"):
        check_transition(current, target)


def test_retry_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(
        max_attempts=5, base_delay=timedelta(seconds=2), max_delay=timedelta(seconds=10)
    )
    assert [policy.delay(n).total_seconds() for n in range(1, 6)] == [2, 4, 8, 10, 10]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay": timedelta(seconds=-1)},
        {"base_delay": timedelta(seconds=10), "max_delay": timedelta(seconds=1)},
    ],
)
def test_retry_policy_rejects_nonsense(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must"):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]
