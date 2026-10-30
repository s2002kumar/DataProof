"""Workers drive claimed runs through their stages under a heartbeated, fenced lease.

A worker claims one run, then executes stage handlers in order until the run reaches
``READY_FOR_REVIEW`` (it parks the run until the review TTL expires), a terminal state, a failed
attempt (the stage is retried later, up to the retry budget) or the loss of its lease (it stops
at once; whoever revoked the lease, or the next claimant, carries on).

The lifecycle itself, not the handlers, decides what happens after an interruption: ``ERROR``,
``CANCELLED`` and ``TIMED_OUT`` go to ``CLEANING``, and cleaning ends the run. Handlers only do
the work of one stage and name the next state; the store rejects any illegal transition.
"""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from dataproof_contracts import ErrorCode, NotExecutedReason, ReviewRun, RunState
from dataproof_control import (
    INTERRUPTED_STATES,
    WORKING_STATES,
    Claim,
    Effects,
    LeaseLostError,
    RetryPolicy,
    RunStore,
    is_terminal,
)

log = logging.getLogger(__name__)

#: Stages that every worker must be able to execute. ``INVESTIGATING`` and ``REPRODUCING`` are
#: optional: a run enters them only if a handler chooses to.
REQUIRED_STAGES = frozenset(
    {
        RunState.FETCHING,
        RunState.ANALYZING,
        RunState.PROVISIONING,
        RunState.MIGRATING,
        RunState.VERIFYING,
        RunState.REPORTING,
    }
)


@dataclass(frozen=True)
class StageOutcome:
    """What a stage produced: the next state and anything to commit with that transition."""

    next_state: RunState
    detail: Mapping[str, Any] = field(default_factory=dict)
    error_code: ErrorCode | None = None
    not_executed_reason: NotExecutedReason | None = None
    isolation_id: str | None = None
    effects: Effects | None = None


class StageContext:
    """Passed to a handler: the run being worked on and a way to stop if the lease is lost."""

    def __init__(self, run: ReviewRun, claim: Claim, lost: threading.Event) -> None:
        self.run = run
        self.claim = claim
        self._lost = lost

    @property
    def lease_lost(self) -> bool:
        return self._lost.is_set()

    def ensure_lease(self) -> None:
        """Raise :class:`LeaseLostError` if a heartbeat found the lease revoked or expired.

        Long-running handlers call this between steps so that a cancelled run stops promptly.
        The store re-checks the lease on every write regardless.
        """
        if self._lost.is_set():
            raise LeaseLostError(f"lease on run {self.claim.run_id} was lost")


StageHandler = Callable[[StageContext], StageOutcome]
#: Removes everything provisioned for the run and returns a cleanup record. Must be idempotent:
#: it may run again after a crash, or for a run that never provisioned anything.
CleanupHandler = Callable[[StageContext], Mapping[str, Any]]


@dataclass(frozen=True)
class WorkerSettings:
    lease: timedelta = timedelta(seconds=60)
    heartbeat_interval: timedelta = timedelta(seconds=15)
    review_ttl: timedelta = timedelta(hours=2)
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        if not timedelta(0) < self.heartbeat_interval < self.lease:
            raise ValueError("heartbeat_interval must be positive and shorter than the lease")


class _Heartbeat:
    """Extends the lease in the background; sets ``lost`` when the lease cannot be extended."""

    def __init__(self, store: RunStore, claim: Claim, settings: WorkerSettings) -> None:
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._store = store
        self._claim = claim
        self._settings = settings
        self._thread = threading.Thread(
            target=self._beat, name=f"heartbeat-{claim.run_id}", daemon=True
        )

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()

    def _beat(self) -> None:
        interval = self._settings.heartbeat_interval.total_seconds()
        while not self._stop.wait(interval):
            try:
                self._store.heartbeat(self._claim, lease=self._settings.lease)
            except LeaseLostError:
                self.lost.set()
                return
            except Exception:
                # A transient database error: keep trying. If the lease expires meanwhile, the
                # next heartbeat reports it lost, and every fenced write already refuses.
                log.warning("heartbeat for run %s failed", self._claim.run_id, exc_info=True)


class Worker:
    def __init__(
        self,
        store: RunStore,
        stages: Mapping[RunState, StageHandler],
        cleanup: CleanupHandler,
        *,
        worker_id: str,
        settings: WorkerSettings | None = None,
    ) -> None:
        missing = REQUIRED_STAGES - stages.keys()
        if missing:
            raise ValueError(f"no handler for stages: {sorted(missing)}")
        unexpected = stages.keys() - (WORKING_STATES - {RunState.QUEUED})
        if unexpected:
            raise ValueError(f"handlers given for non-stage states: {sorted(unexpected)}")
        self._store = store
        self._stages = dict(stages)
        self._cleanup = cleanup
        self.worker_id = worker_id
        self.settings = settings or WorkerSettings()

    def run_once(self) -> bool:
        """Claim one run and drive it as far as this lease allows. Return whether one was found."""
        claim = self._store.claim(self.worker_id, lease=self.settings.lease)
        if claim is None:
            return False
        with _Heartbeat(self._store, claim, self.settings) as heartbeat:
            try:
                self._drive(claim, heartbeat.lost)
            except LeaseLostError:
                log.info("worker %s lost the lease on run %s", self.worker_id, claim.run_id)
        return True

    def sweep(self) -> list[UUID]:
        """Time out runs past their deadline (any worker or the reconciler may do this)."""
        return self._store.expire_overdue(actor=f"worker:{self.worker_id}")

    def _drive(self, claim: Claim, lost: threading.Event) -> None:
        store = self._store
        if claim.budget_exhausted:
            continuing = self._exhausted(claim)
            if continuing is None:
                return
            claim = continuing
        while True:
            state = claim.state
            if state is RunState.QUEUED:
                claim = store.advance(claim, RunState.FETCHING)
                continue
            if state in INTERRUPTED_STATES or state is RunState.READY_FOR_REVIEW:
                claim = store.advance(claim, RunState.CLEANING)
                continue
            context = StageContext(store.get_run(claim.run_id), claim, lost)
            if state is RunState.CLEANING:
                record = self._attempt(claim, self._cleanup, context)
                if record is not None:
                    store.complete_cleaning(claim, detail=record)
                return
            handler = self._stages.get(state)
            if handler is None:
                raise RuntimeError(f"run {claim.run_id} reached {state}, which has no handler")
            outcome = self._attempt(claim, handler, context)
            if outcome is None:
                return
            context.ensure_lease()
            claim = store.advance(
                claim,
                outcome.next_state,
                detail=outcome.detail,
                error_code=outcome.error_code,
                not_executed_reason=outcome.not_executed_reason,
                isolation_id=outcome.isolation_id,
                effects=outcome.effects,
            )
            if outcome.next_state is RunState.READY_FOR_REVIEW:
                store.park(claim, until=self.settings.review_ttl)
                return
            if is_terminal(outcome.next_state):
                return

    def _attempt[T](
        self, claim: Claim, action: Callable[[StageContext], T], context: StageContext
    ) -> T | None:
        """Run one attempt of a stage; on failure release the run for a later retry."""
        try:
            return action(context)
        except LeaseLostError:
            raise
        except Exception as error:
            log.warning(
                "attempt %d of %s for run %s failed",
                claim.attempt,
                claim.state,
                claim.run_id,
                exc_info=True,
            )
            self._store.retry_later(
                claim,
                error=f"{type(error).__name__}: {error}",
                delay=self.settings.retry.delay(claim.attempt),
            )
            return None

    def _exhausted(self, claim: Claim) -> Claim | None:
        """The stage failed on every attempt in its budget: stop retrying it."""
        detail = {
            "attempts": claim.attempt - 1,
            "last_error": self._store.last_error(claim.run_id),
        }
        if claim.state in WORKING_STATES:
            return self._store.advance(
                claim,
                RunState.ERROR,
                error_code=ErrorCode.RETRY_BUDGET_EXHAUSTED,
                detail=detail,
            )
        if claim.state is RunState.CLEANING:
            # Resources may remain; the expiry reconciler keeps removing them by run label.
            self._store.complete_cleaning(
                claim, detail={"cleanup": "FAILED", "reason": "retry budget exhausted", **detail}
            )
            return None
        # READY_FOR_REVIEW and interrupted states only transition; there is nothing to retry.
        return claim
