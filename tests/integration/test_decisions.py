"""Binding decisions and stored configurations in the control database (ADR-0005)."""

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from control_env import FAST_RETRY, NEW_HEAD_SHA, REPOSITORY, RUN_TTL, create, run_inputs
from dataproof_contracts import (
    ApproverSeparation,
    HumanDecision,
    HumanDecisionAction,
    ReviewRun,
    RunTrigger,
)
from dataproof_control import (
    ConfigConflictError,
    DecisionConflictError,
    RecordedDecision,
    RunStore,
    StaleHeadError,
    StoredConfig,
)

pytestmark = pytest.mark.integration

APPROVE = HumanDecisionAction.APPROVE_BINDING
REJECT = HumanDecisionAction.REJECT_BINDING
D1, D2, D3 = (hashlib.sha256(name.encode()).hexdigest() for name in ("b1", "b2", "b3"))
CORRECTION_SHA = "1f2e3d4c5b6a79881f2e3d4c5b6a79881f2e3d4c"


def decide(
    run: ReviewRun,
    action: HumanDecisionAction = APPROVE,
    digest: str | None = D1,
    *,
    actor: str = "maintainer",
) -> HumanDecision:
    return HumanDecision(
        decision_id=uuid4(),
        run_id=run.run_id,
        head_sha=run.inputs.head_sha,
        action=action,
        actor=actor,
        decided_at=datetime.now(UTC),
        binding_digest=digest,
        pr_author="author" if digest else None,
        approver_separation=ApproverSeparation.ORGANIZATIONAL if action is APPROVE else None,
    )


def stored(config: Mapping[str, object], repository: str = REPOSITORY) -> StoredConfig:
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return StoredConfig(digest=digest, repository=repository, config=config)


class Configs:
    """A ``configure`` callback that records what it was asked for."""

    def __init__(self, repository: str = REPOSITORY) -> None:
        self.repository = repository
        self.calls: list[tuple[frozenset[str], frozenset[str]]] = []
        self._lock = threading.Lock()

    def __call__(self, approved: frozenset[str], rejected: frozenset[str]) -> StoredConfig:
        with self._lock:
            self.calls.append((approved, rejected))
        config = {
            "repository": self.repository,
            "approved": sorted(approved),
            "rejected": sorted(rejected),
        }
        return stored(config, self.repository)


def record(
    store: RunStore, decision: HumanDecision, configs: Callable[..., StoredConfig] | None = None
) -> RecordedDecision:
    return store.decide_binding(
        decision, configure=configs or Configs(), run_ttl=RUN_TTL, retry=FAST_RETRY
    )


# Configurations --------------------------------------------------------------------------------


def test_a_configuration_is_stored_once_under_its_digest(store: RunStore) -> None:
    config = stored({"repository": REPOSITORY, "step_timeout_seconds": 300})
    store.save_config(config)
    store.save_config(config)
    assert store.config(config.digest) == config
    assert store.config("0" * 64) is None

    impostor = StoredConfig(
        digest=config.digest,
        repository=REPOSITORY,
        config={"repository": REPOSITORY, "step_timeout_seconds": 1},
    )
    with pytest.raises(ConfigConflictError):
        store.save_config(impostor)
    assert store.config(config.digest) == config


# Binding decisions -----------------------------------------------------------------------------


def test_an_approval_starts_a_child_run_and_leaves_the_original_alone(store: RunStore) -> None:
    original = create(store).run
    log_before = store.transitions(original.run_id)
    configs = Configs()

    decision = decide(original)
    outcome = record(store, decision, configs)

    assert outcome.recorded
    assert outcome.decision == decision
    assert configs.calls == [(frozenset({D1}), frozenset())]
    started = outcome.started
    assert started is not None
    assert started.created
    assert started.superseded == (), "a run started by a decision supersedes nothing"
    child = started.run
    assert child.trigger is RunTrigger.BINDING_APPROVAL
    assert child.parent_run_id == original.run_id
    assert child.inputs == original.inputs.model_copy(
        update={"config_digest": configs(frozenset({D1}), frozenset()).digest}
    )
    assert store.config(child.inputs.config_digest) is not None
    assert store.transitions(child.run_id)[0].actor == "user:maintainer"
    assert store.get_run(original.run_id) == original
    assert store.transitions(original.run_id) == log_before
    assert store.decisions(original.run_id) == [decision]
    assert store.decisions(child.run_id) == []


def test_a_rejection_is_recorded_and_starts_nothing(store: RunStore) -> None:
    original = create(store).run
    configs = Configs()
    outcome = record(store, decide(original, REJECT), configs)
    assert outcome.recorded
    assert outcome.started is None
    assert configs.calls == []
    assert [d.action for d in store.decisions(original.run_id)] == [REJECT]


def test_an_approval_carries_every_decision_made_at_that_head(store: RunStore) -> None:
    original = create(store).run
    record(store, decide(original, REJECT, D3))
    first = record(store, decide(original, APPROVE, D1)).started
    assert first is not None

    configs = Configs()
    # A decision taken on the first approval's run counts toward the same head.
    second = record(store, decide(first.run, APPROVE, D2), configs).started
    assert configs.calls == [(frozenset({D1, D2}), frozenset({D3}))]
    assert second is not None
    assert second.run.parent_run_id == first.run.run_id


def test_a_decision_stands_once_made(store: RunStore) -> None:
    original = create(store).run
    first = record(store, decide(original))
    configs = Configs()

    again = record(store, decide(original, actor="another-maintainer"), configs)
    assert not again.recorded
    assert again.decision == first.decision, "the earlier decision is returned"
    assert first.started is not None
    assert again.started is not None
    assert again.started.run.run_id == first.started.run.run_id
    assert not again.started.created
    assert configs.calls == []

    with pytest.raises(DecisionConflictError, match="APPROVE_BINDING by maintainer"):
        record(store, decide(original, REJECT))
    assert len(store.decisions(original.run_id)) == 1


def test_a_decision_is_bound_to_the_head_of_the_run_it_decides(store: RunStore) -> None:
    original = create(store).run
    decision = decide(original).model_copy(update={"head_sha": NEW_HEAD_SHA})
    with pytest.raises(ValueError, match="bound to the head"):
        record(store, decision)
    assert store.decisions(original.run_id) == []


def test_a_head_the_pull_request_has_left_cannot_be_decided(store: RunStore) -> None:
    old = create(store).run
    create(store, run_inputs(head_sha=NEW_HEAD_SHA))
    with pytest.raises(StaleHeadError, match=NEW_HEAD_SHA):
        record(store, decide(old))
    assert store.decisions(old.run_id) == []


def test_a_correction_after_the_latest_push_can_be_decided(store: RunStore) -> None:
    pushed = create(store).run
    correction = store.start_correction_replay(
        pushed.run_id,
        head_sha=CORRECTION_SHA,
        config_digest="c" * 64,
        actor="user:developer",
        run_ttl=RUN_TTL,
        retry=FAST_RETRY,
    ).run
    assert record(store, decide(correction)).recorded
    assert record(store, decide(pushed, digest=D2)).recorded, "the pushed head is still current"

    create(store, run_inputs(head_sha=NEW_HEAD_SHA))
    with pytest.raises(StaleHeadError):
        record(store, decide(correction, digest=D2))


def test_only_binding_decisions_are_recorded_here(store: RunStore) -> None:
    original = create(store).run
    with pytest.raises(ValueError, match="not a binding decision"):
        record(store, decide(original, HumanDecisionAction.CANCEL_RUN, None))


def test_a_configuration_for_another_repository_is_refused(store: RunStore) -> None:
    original = create(store).run
    with pytest.raises(ValueError, match="another repository"):
        record(store, decide(original), Configs("example-org/elsewhere"))
    assert store.decisions(original.run_id) == []


def test_concurrent_approvals_at_one_head_are_serialized(store: RunStore) -> None:
    original = create(store).run
    configs = Configs()
    barrier = threading.Barrier(2)

    def approve(digest: str) -> RecordedDecision:
        barrier.wait()
        return record(store, decide(original, APPROVE, digest), configs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, [D1, D2]))

    assert all(outcome.recorded for outcome in outcomes)
    assert sorted(len(approved) for approved, _ in configs.calls) == [1, 2], (
        "the later approval sees the earlier one"
    )
