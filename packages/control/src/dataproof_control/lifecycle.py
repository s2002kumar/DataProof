"""The run state machine: every legal transition and nothing else (ARCHITECTURE).

The happy path is ``QUEUED -> FETCHING -> ANALYZING -> PROVISIONING -> MIGRATING -> VERIFYING ->
(INVESTIGATING -> REPRODUCING?) -> REPORTING -> READY_FOR_REVIEW -> CLEANING -> COMPLETE``.
``ANALYZING`` may end the run as ``NOT_EXECUTED`` (a policy denial; nothing was provisioned).

Any working state may be interrupted by ``ERROR``, ``CANCELLED`` or ``TIMED_OUT``; a run in
``READY_FOR_REVIEW`` may be ``CANCELLED`` (manually, or because a newer head SHA superseded it).
Every interruption leads to ``CLEANING``, and cleaning ends in ``COMPLETE`` or, when a newer run
superseded this one, ``SUPERSEDED``. Cleanup never rewrites verification outcomes.

There is no binding-review state: a run never waits for a human.
"""

from collections.abc import Mapping
from types import MappingProxyType

from dataproof_contracts import RunState
from dataproof_contracts.enums import TERMINAL_RUN_STATES

S = RunState

#: States in which a worker performs pipeline work. Any of them may be interrupted.
WORKING_STATES = frozenset(
    {
        S.QUEUED,
        S.FETCHING,
        S.ANALYZING,
        S.PROVISIONING,
        S.MIGRATING,
        S.VERIFYING,
        S.INVESTIGATING,
        S.REPRODUCING,
        S.REPORTING,
    }
)

#: States that end the pipeline early and must be followed by cleanup.
INTERRUPTED_STATES = frozenset({S.ERROR, S.CANCELLED, S.TIMED_OUT})

#: States that a cancellation (requested or by supersession) may interrupt.
CANCELLABLE_STATES = WORKING_STATES | {S.READY_FOR_REVIEW}

_FORWARD: dict[RunState, frozenset[RunState]] = {
    S.QUEUED: frozenset({S.FETCHING}),
    S.FETCHING: frozenset({S.ANALYZING}),
    S.ANALYZING: frozenset({S.PROVISIONING, S.NOT_EXECUTED}),
    S.PROVISIONING: frozenset({S.MIGRATING}),
    S.MIGRATING: frozenset({S.VERIFYING}),
    S.VERIFYING: frozenset({S.INVESTIGATING, S.REPORTING}),
    S.INVESTIGATING: frozenset({S.REPRODUCING, S.REPORTING}),
    S.REPRODUCING: frozenset({S.REPORTING}),
    S.REPORTING: frozenset({S.READY_FOR_REVIEW}),
    S.READY_FOR_REVIEW: frozenset({S.CLEANING, S.CANCELLED}),
    S.CLEANING: frozenset({S.COMPLETE, S.SUPERSEDED}),
}


def _build() -> Mapping[RunState, frozenset[RunState]]:
    table = {state: set(_FORWARD.get(state, frozenset())) for state in RunState}
    for state in WORKING_STATES:
        table[state] |= INTERRUPTED_STATES
    for state in INTERRUPTED_STATES:
        table[state].add(S.CLEANING)
    return MappingProxyType({state: frozenset(targets) for state, targets in table.items()})


#: The complete transition table. The control database holds the same table and rejects any
#: logged transition that is not in it (see the store's schema).
TRANSITIONS: Mapping[RunState, frozenset[RunState]] = _build()

#: The state a run is created in.
INITIAL_STATE = S.QUEUED


class IllegalTransitionError(ValueError):
    def __init__(self, current: RunState, target: RunState) -> None:
        super().__init__(f"illegal run transition {current} -> {target}")
        self.current = current
        self.target = target


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_RUN_STATES


def check_transition(current: RunState, target: RunState) -> None:
    """Raise unless ``current -> target`` is in the transition table."""
    if target not in TRANSITIONS[current]:
        raise IllegalTransitionError(current, target)
