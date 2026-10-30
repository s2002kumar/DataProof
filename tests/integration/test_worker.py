"""Workers drive runs through the lifecycle: retries, crashes, cancellation, timeouts, TTLs."""

import threading
import time
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from control_env import (
    FAST_RETRY,
    LEASE,
    NEW_HEAD_SHA,
    create,
    expire_lease,
    make_due,
    pass_deadline,
    run_inputs,
)
from dataproof_contracts import ErrorCode, NotExecutedReason, RunState
from dataproof_control import RunStore
from dataproof_runner import StageContext, StageHandler, StageOutcome, Worker, WorkerSettings

pytestmark = pytest.mark.integration
S = RunState

NEXT = {
    S.FETCHING: S.ANALYZING,
    S.ANALYZING: S.PROVISIONING,
    S.PROVISIONING: S.MIGRATING,
    S.MIGRATING: S.VERIFYING,
    S.VERIFYING: S.REPORTING,
    S.REPORTING: S.READY_FOR_REVIEW,
}
FULL_PATH = [
    S.QUEUED,
    S.FETCHING,
    S.ANALYZING,
    S.PROVISIONING,
    S.MIGRATING,
    S.VERIFYING,
    S.REPORTING,
    S.READY_FOR_REVIEW,
    S.CLEANING,
    S.COMPLETE,
]
SETTINGS = WorkerSettings(
    lease=LEASE,
    heartbeat_interval=timedelta(seconds=5),
    review_ttl=timedelta(hours=1),
    retry=FAST_RETRY,
)


class Pipeline:
    """Stage handlers that record what ran; individual stages can be replaced per test."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, RunState]] = []
        self.cleaned: list[UUID] = []
        self.overrides: dict[RunState, StageHandler] = {}
        self.cleanup_failures = 0

    def handlers(self) -> dict[RunState, StageHandler]:
        return {state: self._handler(state) for state in NEXT}

    def _handler(self, state: RunState) -> StageHandler:
        def handle(context: StageContext) -> StageOutcome:
            self.calls.append((context.run.run_id, state))
            if state in self.overrides:
                return self.overrides[state](context)
            return StageOutcome(NEXT[state])

        return handle

    def cleanup(self, context: StageContext) -> Mapping[str, Any]:
        if self.cleanup_failures:
            self.cleanup_failures -= 1
            raise RuntimeError("docker daemon unavailable")
        self.cleaned.append(context.run.run_id)
        return {"cleanup": "SUCCEEDED"}

    def worker(
        self, store: RunStore, name: str = "w1", settings: WorkerSettings = SETTINGS
    ) -> Worker:
        return Worker(store, self.handlers(), self.cleanup, worker_id=name, settings=settings)


def _states(store: RunStore, run_id: UUID) -> list[RunState]:
    return [t.to_state for t in store.transitions(run_id)]


def _drain(worker: Worker) -> int:
    claims = 0
    while worker.run_once():
        claims += 1
    return claims


def test_a_run_goes_through_every_stage_waits_for_review_then_cleans_up(
    store: RunStore, control_dsn: str
) -> None:
    run = create(store).run
    pipeline = Pipeline()
    worker = pipeline.worker(store)

    assert worker.run_once()
    assert store.get_run(run.run_id).state is S.READY_FOR_REVIEW
    assert not worker.run_once(), "a run in review is parked until its review TTL expires"
    assert pipeline.cleaned == []

    make_due(control_dsn, run.run_id)
    assert worker.run_once()
    assert _states(store, run.run_id) == FULL_PATH
    assert pipeline.cleaned == [run.run_id]
    assert [state for _, state in pipeline.calls] == list(NEXT)
    assert store.get_run(run.run_id).lease is None
    assert not worker.run_once()


def test_investigation_is_entered_only_when_a_stage_asks_for_it(store: RunStore) -> None:
    run = create(store).run
    pipeline = Pipeline()
    pipeline.overrides[S.VERIFYING] = lambda _: StageOutcome(S.INVESTIGATING)
    handlers = pipeline.handlers()
    handlers[S.INVESTIGATING] = lambda _: StageOutcome(S.REPORTING, detail={"hypotheses": 1})
    worker = Worker(store, handlers, pipeline.cleanup, worker_id="w1", settings=SETTINGS)
    worker.run_once()
    states = _states(store, run.run_id)
    assert states[states.index(S.VERIFYING) + 1 :] == [
        S.INVESTIGATING,
        S.REPORTING,
        S.READY_FOR_REVIEW,
    ]


def test_policy_denial_ends_the_run_without_provisioning_or_cleanup(store: RunStore) -> None:
    run = create(store).run
    pipeline = Pipeline()
    pipeline.overrides[S.ANALYZING] = lambda _: StageOutcome(
        S.NOT_EXECUTED,
        not_executed_reason=NotExecutedReason.DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL,
        detail={"paths": ["sample-app/uv.lock"]},
    )
    worker = pipeline.worker(store)
    assert worker.run_once()
    ended = store.get_run(run.run_id)
    assert ended.state is S.NOT_EXECUTED
    assert ended.not_executed_reason is NotExecutedReason.DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL
    assert [state for _, state in pipeline.calls] == [S.FETCHING, S.ANALYZING]
    assert pipeline.cleaned == []
    assert not worker.run_once()


def test_a_failed_attempt_is_retried_in_place(store: RunStore) -> None:
    run = create(store).run
    pipeline = Pipeline()
    failures = iter([RuntimeError("image pull timed out")])

    def flaky(context: StageContext) -> StageOutcome:
        error = next(failures, None)
        if error is not None:
            raise error
        return StageOutcome(S.MIGRATING)

    pipeline.overrides[S.PROVISIONING] = flaky
    worker = pipeline.worker(store)

    assert worker.run_once()
    assert store.get_run(run.run_id).state is S.PROVISIONING
    assert store.last_error(run.run_id) == "RuntimeError: image pull timed out"

    assert worker.run_once()
    assert store.get_run(run.run_id).state is S.READY_FOR_REVIEW
    assert Counter(state for _, state in pipeline.calls)[S.PROVISIONING] == 2
    states = _states(store, run.run_id)
    assert states.count(S.PROVISIONING) == 1, "a retry repeats a stage without a new transition"


def test_exhausting_the_retry_budget_errors_the_run_and_still_cleans_up(store: RunStore) -> None:
    run = create(store).run
    pipeline = Pipeline()

    def broken(context: StageContext) -> StageOutcome:
        raise RuntimeError("PostgreSQL never became ready")

    pipeline.overrides[S.PROVISIONING] = broken
    worker = pipeline.worker(store)

    assert _drain(worker) == FAST_RETRY.max_attempts + 1
    ended = store.get_run(run.run_id)
    assert ended.state is S.COMPLETE
    assert ended.error_code is ErrorCode.RETRY_BUDGET_EXHAUSTED
    assert Counter(state for _, state in pipeline.calls)[S.PROVISIONING] == FAST_RETRY.max_attempts
    assert pipeline.cleaned == [run.run_id]
    log = store.transitions(run.run_id)
    error = next(t for t in log if t.to_state is S.ERROR)
    assert error.detail == {
        "attempts": FAST_RETRY.max_attempts,
        "last_error": "RuntimeError: PostgreSQL never became ready",
    }
    assert [t.to_state for t in log][-3:] == [S.ERROR, S.CLEANING, S.COMPLETE]


def test_a_crashed_worker_is_replaced_after_its_lease_expires(
    store: RunStore, control_dsn: str
) -> None:
    run = create(store).run
    crashed = store.claim("crashed", lease=LEASE)
    assert crashed is not None
    store.advance(crashed, S.FETCHING)  # it got this far, then died without releasing the lease

    pipeline = Pipeline()
    worker = pipeline.worker(store, "w2")
    assert not worker.run_once(), "a live lease is respected"
    expire_lease(control_dsn, run.run_id)
    assert worker.run_once()
    assert store.get_run(run.run_id).state is S.READY_FOR_REVIEW
    assert pipeline.calls[0] == (run.run_id, S.FETCHING)


def test_cancelling_mid_stage_stops_the_worker_and_discards_the_stage_result(
    store: RunStore,
) -> None:
    run = create(store).run
    pipeline = Pipeline()
    entered, release = threading.Event(), threading.Event()

    def slow_migration(context: StageContext) -> StageOutcome:
        entered.set()
        release.wait(10)
        return StageOutcome(S.VERIFYING, detail={"migrated": True})

    pipeline.overrides[S.MIGRATING] = slow_migration
    worker = pipeline.worker(store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        driving = pool.submit(worker.run_once)
        assert entered.wait(10)
        assert store.cancel(run.run_id, actor="user:alice")
        release.set()
        assert driving.result(10)

    assert store.get_run(run.run_id).state is S.CANCELLED
    assert S.VERIFYING not in _states(store, run.run_id)
    assert pipeline.cleaned == []

    assert worker.run_once()
    assert _states(store, run.run_id)[-3:] == [S.CANCELLED, S.CLEANING, S.COMPLETE]
    assert pipeline.cleaned == [run.run_id]


def test_a_handler_that_checks_its_lease_stops_promptly_after_cancellation(
    store: RunStore,
) -> None:
    run = create(store).run
    pipeline = Pipeline()
    entered, stopped = threading.Event(), threading.Event()

    def cooperative(context: StageContext) -> StageOutcome:
        entered.set()
        try:
            for _ in range(200):
                context.ensure_lease()
                time.sleep(0.05)
        except BaseException:
            stopped.set()
            raise
        return StageOutcome(S.VERIFYING)

    pipeline.overrides[S.MIGRATING] = cooperative
    settings = WorkerSettings(
        lease=timedelta(seconds=5), heartbeat_interval=timedelta(milliseconds=100), retry=FAST_RETRY
    )
    worker = pipeline.worker(store, settings=settings)
    with ThreadPoolExecutor(max_workers=1) as pool:
        driving = pool.submit(worker.run_once)
        assert entered.wait(10)
        store.cancel(run.run_id, actor="user:alice")
        assert stopped.wait(2), "the next heartbeat should reveal the revoked lease"
        assert driving.result(10)
    assert store.get_run(run.run_id).state is S.CANCELLED


def test_heartbeats_keep_a_stage_longer_than_the_lease_alive(store: RunStore) -> None:
    run = create(store).run
    pipeline = Pipeline()

    def slow(context: StageContext) -> StageOutcome:
        time.sleep(2.5)
        return StageOutcome(S.REPORTING)

    pipeline.overrides[S.VERIFYING] = slow
    settings = WorkerSettings(
        lease=timedelta(seconds=1), heartbeat_interval=timedelta(milliseconds=200), retry=FAST_RETRY
    )
    worker = pipeline.worker(store, settings=settings)
    rival = pipeline.worker(store, "rival", settings)
    with ThreadPoolExecutor(max_workers=1) as pool:
        driving = pool.submit(worker.run_once)
        time.sleep(1.5)
        assert not rival.run_once(), "the heartbeated lease must not be claimable"
        assert driving.result(10)
    assert store.get_run(run.run_id).state is S.READY_FOR_REVIEW
    assert {t.actor for t in store.transitions(run.run_id)[1:]} == {"worker:w1"}


def test_an_overdue_run_times_out_and_is_cleaned_up(store: RunStore, control_dsn: str) -> None:
    run = create(store).run
    pipeline = Pipeline()
    worker = pipeline.worker(store)
    pass_deadline(control_dsn, run.run_id)
    assert worker.sweep() == [run.run_id]
    assert worker.run_once()
    assert _states(store, run.run_id) == [S.QUEUED, S.TIMED_OUT, S.CLEANING, S.COMPLETE]
    assert pipeline.calls == []
    assert pipeline.cleaned == [run.run_id]


def test_a_superseded_run_in_review_is_torn_down_and_ends_superseded(
    store: RunStore,
) -> None:
    old = create(store).run
    pipeline = Pipeline()
    worker = pipeline.worker(store)
    worker.run_once()
    assert store.get_run(old.run_id).state is S.READY_FOR_REVIEW

    new = create(store, run_inputs(head_sha=NEW_HEAD_SHA)).run
    _drain(worker)
    ended = store.get_run(old.run_id)
    assert ended.state is S.SUPERSEDED
    assert ended.superseded_by == new.run_id
    assert _states(store, old.run_id)[-4:] == [
        S.READY_FOR_REVIEW,
        S.CANCELLED,
        S.CLEANING,
        S.SUPERSEDED,
    ]
    assert old.run_id in pipeline.cleaned
    assert store.get_run(new.run_id).state is S.READY_FOR_REVIEW


def test_failed_cleanup_is_retried(store: RunStore) -> None:
    run = create(store).run
    store.cancel(run.run_id, actor="user:alice")
    pipeline = Pipeline()
    pipeline.cleanup_failures = 1
    worker = pipeline.worker(store)
    assert _drain(worker) == 2
    assert store.get_run(run.run_id).state is S.COMPLETE
    assert pipeline.cleaned == [run.run_id]


def test_cleanup_that_never_succeeds_still_ends_the_run_and_says_so(store: RunStore) -> None:
    run = create(store).run
    store.cancel(run.run_id, actor="user:alice")
    pipeline = Pipeline()
    pipeline.cleanup_failures = 99
    worker = pipeline.worker(store)
    assert _drain(worker) == FAST_RETRY.max_attempts + 1
    ended = store.transitions(run.run_id)[-1]
    assert ended.to_state is S.COMPLETE
    assert ended.detail["cleanup"] == "FAILED"
    assert ended.detail["last_error"] == "RuntimeError: docker daemon unavailable"


def test_worker_requires_a_handler_for_every_stage(store: RunStore) -> None:
    pipeline = Pipeline()
    handlers = pipeline.handlers()
    del handlers[S.MIGRATING]
    with pytest.raises(ValueError, match="MIGRATING"):
        Worker(store, handlers, pipeline.cleanup, worker_id="w1")
    handlers = pipeline.handlers()
    handlers[S.QUEUED] = lambda _: StageOutcome(S.FETCHING)
    with pytest.raises(ValueError, match="non-stage"):
        Worker(store, handlers, pipeline.cleanup, worker_id="w1")


def test_heartbeat_must_be_shorter_than_the_lease() -> None:
    with pytest.raises(ValueError, match="heartbeat_interval"):
        WorkerSettings(lease=timedelta(seconds=5), heartbeat_interval=timedelta(seconds=5))
