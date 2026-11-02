"""PostgreSQL-backed run store: idempotent creation, fenced transitions and the durable job queue.

Concurrency model:

* A run is created once per input digest (the idempotency key); a racing duplicate gets the
  existing run back.
* A worker claims a run's job with ``FOR UPDATE SKIP LOCKED``. The claim increments the job's
  fencing token and attempt counter and leases the job for a fixed duration, which the worker
  extends by heartbeats.
* Every worker write (transition, heartbeat, park, retry) re-checks, in the same transaction,
  that the worker still owns the lease, under the same token, and that it has not expired. A worker
  that fails this check gets :class:`LeaseLostError` and must stop without writing anything else.
* Cancellation, timeout and supersession are made by trusted callers that do not hold the lease.
  They transition the run and increment the fencing token, which revokes the current holder.
* All times come from the database clock, so workers' clocks cannot disagree about leases.

Rows are always locked in the order ``runs`` then ``run_jobs``. A claim locks only the job and
skips locked rows, so it can never wait on a run lock.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from dataproof_contracts import (
    ErrorCode,
    HumanDecision,
    HumanDecisionAction,
    NotExecutedReason,
    ReviewRun,
    RunInputs,
    RunState,
    RunTrigger,
    WorkerLease,
)
from dataproof_contracts.enums import BINDING_DECISIONS
from dataproof_control.lifecycle import (
    CANCELLABLE_STATES,
    INITIAL_STATE,
    WORKING_STATES,
    check_transition,
    is_terminal,
)

Connection = psycopg.Connection[DictRow]

#: Work done in the same transaction as a fenced transition, so that results and the state that
#: records them are committed together or not at all.
Effects = Callable[[Connection], None]

_MAX_ERROR_TEXT = 2000
#: Triggers whose runs start from a new pull request head and supersede older heads. Runs that a
#: decision derives from an earlier run supersede no other head; an approval run supersedes only
#: approval runs at its own head that have not started (``decide_binding``).
_SUPERSEDING_TRIGGERS = frozenset({RunTrigger.WEBHOOK, RunTrigger.MANUAL})


def _required(row: DictRow | None, what: str) -> DictRow:
    if row is None:
        raise RuntimeError(f"control database invariant violated: {what}")
    return row


class LeaseLostError(RuntimeError):
    """The caller no longer holds the run's lease and must stop without writing."""


class RunNotFoundError(LookupError):
    pass


class StaleHeadError(RuntimeError):
    """The pull request has moved to a newer head than the one the decision is bound to."""


class DecisionConflictError(RuntimeError):
    """A different decision on this binding was already recorded at this head; it stands."""


class ConfigConflictError(RuntimeError):
    """A different configuration is already stored under this digest."""


@dataclass(frozen=True)
class RetryPolicy:
    """Per-stage attempt budget with capped exponential backoff."""

    max_attempts: int = 3
    base_delay: timedelta = timedelta(seconds=5)
    max_delay: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < timedelta(0) or self.max_delay < self.base_delay:
            raise ValueError("delays must satisfy 0 <= base_delay <= max_delay")

    def delay(self, attempt: int) -> timedelta:
        """Backoff before retrying after the given (1-based) attempt failed."""
        return min(self.base_delay * (1 << max(attempt - 1, 0)), self.max_delay)


@dataclass(frozen=True)
class Claim:
    """A worker's lease on one run. ``attempt`` counts claims of the current stage."""

    run_id: UUID
    worker_id: str
    fencing_token: int
    state: RunState
    attempt: int
    max_attempts: int

    @property
    def budget_exhausted(self) -> bool:
        return self.attempt > self.max_attempts


@dataclass(frozen=True)
class CreatedRun:
    run: ReviewRun
    created: bool
    superseded: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class StoredConfig:
    """A project configuration stored under its digest.

    The control plane does not interpret the configuration. Whoever loads one validates it again
    and checks that it hashes to its digest before running anything under it.
    """

    digest: str
    repository: str
    config: Mapping[str, Any]


#: Builds the configuration for a binding-approval run from the digests approved and rejected so
#: far at the head decided on (this decision included).
Configure = Callable[[frozenset[str], frozenset[str]], StoredConfig]


@dataclass(frozen=True)
class RecordedDecision:
    """A binding decision as recorded. ``recorded`` is False when the same decision already
    stood, in which case the earlier one, and the run it started, are returned."""

    decision: HumanDecision
    recorded: bool
    started: CreatedRun | None


@dataclass(frozen=True)
class Transition:
    seq: int
    from_state: RunState | None
    to_state: RunState
    actor: str
    fencing_token: int | None
    detail: Mapping[str, Any]
    at: datetime


class RunStore:
    """Run records and their job queue in the control database (schema: ``migrate``)."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with (
            psycopg.connect(self._conninfo, autocommit=True, row_factory=dict_row) as conn,
            conn.transaction(),
        ):
            yield conn

    # Creation and supersession ---------------------------------------------------------------

    def create_run(
        self,
        inputs: RunInputs,
        trigger: RunTrigger,
        *,
        actor: str,
        run_ttl: timedelta,
        retry: RetryPolicy,
        parent_run_id: UUID | None = None,
    ) -> CreatedRun:
        """Create the run for these inputs, or return the existing one (idempotent).

        A new webhook or manual run for a pull request supersedes every open run of that pull
        request bound to a different head SHA.
        """
        with self._transaction() as conn:
            return self._create(
                conn,
                inputs,
                trigger,
                actor=actor,
                run_ttl=run_ttl,
                retry=retry,
                parent_run_id=parent_run_id,
            )

    def _lock_pull_request(
        self, conn: Connection, repository: str, pull_request: int | None
    ) -> None:
        """Serialize run creation, decisions and closing per pull request (until commit)."""
        if pull_request is not None:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{repository}#{pull_request}",),
            )

    def _create(
        self,
        conn: Connection,
        inputs: RunInputs,
        trigger: RunTrigger,
        *,
        actor: str,
        run_ttl: timedelta,
        retry: RetryPolicy,
        parent_run_id: UUID | None,
    ) -> CreatedRun:
        # Serialized per pull request so that the later head always wins.
        self._lock_pull_request(conn, inputs.repository, inputs.pull_request)
        run_id = uuid4()
        inserted = conn.execute(
            """
            INSERT INTO runs (run_id, input_digest, inputs, repository, pull_request,
                              head_sha, trigger, parent_run_id, state, deadline_at,
                              created_at, updated_at)
            VALUES (%(run_id)s, %(digest)s, %(inputs)s, %(repository)s, %(pull_request)s,
                    %(head_sha)s, %(trigger)s, %(parent)s, %(state)s,
                    statement_timestamp() + %(ttl)s,
                    statement_timestamp(), statement_timestamp())
            ON CONFLICT (input_digest) DO NOTHING
            RETURNING run_id
            """,
            {
                "run_id": run_id,
                "digest": inputs.digest(),
                "inputs": Jsonb(inputs.model_dump(mode="json")),
                "repository": inputs.repository,
                "pull_request": inputs.pull_request,
                "head_sha": inputs.head_sha,
                "trigger": trigger.value,
                "parent": parent_run_id,
                "state": INITIAL_STATE.value,
                "ttl": run_ttl,
            },
        ).fetchone()
        if inserted is None:
            existing = _required(
                conn.execute(
                    "SELECT run_id FROM runs WHERE input_digest = %s", (inputs.digest(),)
                ).fetchone(),
                "a conflicting input digest belongs to an existing run",
            )
            return CreatedRun(self._load(conn, existing["run_id"]), created=False)
        self._log(conn, run_id, None, INITIAL_STATE, actor=actor, token=None, detail={})
        conn.execute(
            "INSERT INTO run_jobs (run_id, available_at, max_attempts) "
            "VALUES (%s, statement_timestamp(), %s)",
            (run_id, retry.max_attempts),
        )
        superseded: tuple[UUID, ...] = ()
        if inputs.pull_request is not None and trigger in _SUPERSEDING_TRIGGERS:
            superseded = self._supersede(conn, run_id, inputs, actor=actor)
        return CreatedRun(self._load(conn, run_id), created=True, superseded=superseded)

    def start_correction_replay(
        self,
        original_run_id: UUID,
        *,
        head_sha: str,
        config_digest: str,
        actor: str,
        run_ttl: timedelta,
        retry: RetryPolicy,
        decision: HumanDecision | None = None,
    ) -> CreatedRun:
        """Evaluate a developer's correction as a new run (ARCHITECTURE, correction replay).

        The new run keeps the original's repository, pull request, base commit, dataset and
        engine version; its head is the correction commit and its configuration digest the one
        that approves the correction's bindings (approvals are bound to the head commit). It
        records the original as its parent, supersedes nothing, and leaves the original run, its
        report and its evidence exactly as they are. ``decision``, a
        ``REQUEST_CORRECTION_REPLAY`` on the original naming the correction commit, is recorded
        with the run it started, in the same transaction.
        """
        if decision is not None and (
            decision.action is not HumanDecisionAction.REQUEST_CORRECTION_REPLAY
            or decision.run_id != original_run_id
            or decision.head_sha != head_sha
        ):
            raise ValueError("the decision does not request this correction of this run")
        with self._transaction() as conn:
            original = self._load(conn, original_run_id)
            if head_sha == original.inputs.head_sha:
                raise ValueError("a correction must be a different commit from the one it corrects")
            inputs = original.inputs.model_copy(
                update={"head_sha": head_sha, "config_digest": config_digest}
            )
            RunInputs.model_validate(inputs.model_dump())
            created = self._create(
                conn,
                inputs,
                RunTrigger.CORRECTION_REPLAY,
                actor=actor,
                run_ttl=run_ttl,
                retry=retry,
                parent_run_id=original_run_id,
            )
            if decision is not None:
                self._insert_decision(
                    conn, decision, original.inputs.repository, created.run.run_id
                )
            return created

    # Configurations and decisions ------------------------------------------------------------

    def save_config(self, config: StoredConfig) -> None:
        """Store a configuration under its digest (idempotent; a stored one never changes)."""
        with self._transaction() as conn:
            self._save_config(conn, config)

    def _save_config(self, conn: Connection, config: StoredConfig) -> None:
        conn.execute(
            "INSERT INTO project_configs (config_digest, repository, config) VALUES (%s, %s, %s) "
            "ON CONFLICT (config_digest) DO NOTHING",
            (config.digest, config.repository, Jsonb(dict(config.config))),
        )
        stored = _required(
            conn.execute(
                "SELECT repository, config FROM project_configs WHERE config_digest = %s",
                (config.digest,),
            ).fetchone(),
            "a stored configuration is missing",
        )
        if (stored["repository"], stored["config"]) != (config.repository, dict(config.config)):
            raise ConfigConflictError(f"another configuration has digest {config.digest}")

    def config(self, digest: str) -> StoredConfig | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT repository, config FROM project_configs WHERE config_digest = %s",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return StoredConfig(digest=digest, repository=row["repository"], config=row["config"])

    def decide_binding(
        self,
        decision: HumanDecision,
        *,
        configure: Configure,
        run_ttl: timedelta,
        retry: RetryPolicy,
    ) -> RecordedDecision:
        """Record ``APPROVE_BINDING`` or ``REJECT_BINDING`` on a run (ADR-0005).

        The decision is bound to the head of the run decided on and to the binding's digest, and
        that head must still be current for the pull request. The first decision on a binding at
        a head stands: repeating it returns it, contradicting it is refused. An approval starts a
        new run (``binding_approval``, parent = the run decided on) under the configuration that
        ``configure`` builds from every approval and rejection at that head. The run decided on
        is never resumed or changed. The new run supersedes only the approval runs at this head
        that are still queued: its configuration carries every approval theirs did, so running
        them would repeat the work for a strictly older configuration. A reviewer who approves
        five bindings starts one run that completes the review, not five. Approval runs that
        have started keep running and keep their results. Authorization and the approver rule
        are the caller's: this records what an authorized reviewer decided.
        """
        if decision.action not in BINDING_DECISIONS:
            raise ValueError(f"{decision.action} is not a binding decision")
        with self._transaction() as conn:
            run = self._load(conn, decision.run_id)
            inputs = run.inputs
            self._lock_pull_request(conn, inputs.repository, inputs.pull_request)
            if decision.head_sha != inputs.head_sha:
                raise ValueError("a binding decision is bound to the head of the run it decides on")
            self._ensure_current_head(conn, inputs)
            at_head = conn.execute(
                "SELECT action, binding_digest, decision, started_run_id FROM human_decisions "
                "WHERE repository = %s AND head_sha = %s AND binding_digest IS NOT NULL",
                (inputs.repository, inputs.head_sha),
            ).fetchall()
            for row in at_head:
                if row["binding_digest"] != decision.binding_digest:
                    continue
                earlier = HumanDecision.model_validate(row["decision"])
                if earlier.action is not decision.action:
                    raise DecisionConflictError(
                        f"{earlier.action} by {earlier.actor} already stands for this binding"
                    )
                started = None
                if row["started_run_id"] is not None:
                    started = CreatedRun(self._load(conn, row["started_run_id"]), created=False)
                return RecordedDecision(earlier, recorded=False, started=started)

            created = None
            if decision.action is HumanDecisionAction.APPROVE_BINDING:
                approved = {
                    r["binding_digest"] for r in at_head if r["action"] == "APPROVE_BINDING"
                }
                rejected = {r["binding_digest"] for r in at_head if r["action"] == "REJECT_BINDING"}
                approved.add(decision.binding_digest)
                config = configure(frozenset(approved), frozenset(rejected))
                if config.repository != inputs.repository:
                    raise ValueError("the configuration belongs to another repository")
                self._save_config(conn, config)
                created = self._create(
                    conn,
                    inputs.model_copy(update={"config_digest": config.digest}),
                    RunTrigger.BINDING_APPROVAL,
                    actor=f"user:{decision.actor}",
                    run_ttl=run_ttl,
                    retry=retry,
                    parent_run_id=run.run_id,
                )
                superseded = self._supersede_queued_approvals(
                    conn, created.run.run_id, inputs, actor=f"user:{decision.actor}"
                )
                created = replace(created, superseded=superseded)
            self._insert_decision(
                conn, decision, inputs.repository, created.run.run_id if created else None
            )
            return RecordedDecision(decision, recorded=True, started=created)

    def _insert_decision(
        self,
        conn: Connection,
        decision: HumanDecision,
        repository: str,
        started_run_id: UUID | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO human_decisions (decision_id, run_id, repository, head_sha, action,
                                         actor, binding_digest, started_run_id, decision)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                decision.decision_id,
                decision.run_id,
                repository,
                decision.head_sha,
                decision.action.value,
                decision.actor,
                decision.binding_digest,
                started_run_id,
                Jsonb(decision.model_dump(mode="json")),
            ),
        )

    def decisions(self, run_id: UUID) -> list[HumanDecision]:
        """The decisions recorded on a run, oldest first."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT decision FROM human_decisions WHERE run_id = %s "
                "ORDER BY recorded_at, decision_id",
                (run_id,),
            ).fetchall()
        return [HumanDecision.model_validate(row["decision"]) for row in rows]

    def _ensure_current_head(self, conn: Connection, inputs: RunInputs) -> None:
        """Refuse a decision on a head that the pull request has moved past.

        The current head is the one of the pull request's latest webhook or manual run, or of a
        correction replay requested after it.
        """
        if inputs.pull_request is None:
            return
        latest = conn.execute(
            """
            SELECT head_sha, created_at FROM runs
            WHERE repository = %s AND pull_request = %s AND trigger = ANY(%s)
            ORDER BY created_at DESC, run_id DESC LIMIT 1
            """,
            (
                inputs.repository,
                inputs.pull_request,
                [trigger.value for trigger in _SUPERSEDING_TRIGGERS],
            ),
        ).fetchone()
        if latest is None or latest["head_sha"] == inputs.head_sha:
            return
        correction = conn.execute(
            """
            SELECT 1 FROM runs
            WHERE repository = %s AND pull_request = %s AND head_sha = %s
              AND trigger = %s AND created_at > %s
            LIMIT 1
            """,
            (
                inputs.repository,
                inputs.pull_request,
                inputs.head_sha,
                RunTrigger.CORRECTION_REPLAY.value,
                latest["created_at"],
            ),
        ).fetchone()
        if correction is None:
            raise StaleHeadError(
                f"pull request #{inputs.pull_request} has moved to {latest['head_sha']}"
            )

    def _supersede(
        self, conn: Connection, new_run: UUID, inputs: RunInputs, *, actor: str
    ) -> tuple[UUID, ...]:
        rows = conn.execute(
            """
            SELECT run_id, state FROM runs
            WHERE repository = %s AND pull_request = %s AND head_sha <> %s
              AND superseded_by IS NULL
              AND state NOT IN ('COMPLETE', 'SUPERSEDED', 'NOT_EXECUTED')
            ORDER BY created_at, run_id
            FOR UPDATE
            """,
            (inputs.repository, inputs.pull_request, inputs.head_sha),
        ).fetchall()
        return self._retire(conn, new_run, rows, actor=actor)

    def _supersede_queued_approvals(
        self, conn: Connection, new_run: UUID, inputs: RunInputs, *, actor: str
    ) -> tuple[UUID, ...]:
        rows = conn.execute(
            """
            SELECT run_id, state FROM runs
            WHERE repository = %s AND pull_request = %s AND head_sha = %s
              AND trigger = 'binding_approval' AND run_id <> %s
              AND superseded_by IS NULL AND state = 'QUEUED'
            ORDER BY created_at, run_id
            FOR UPDATE
            """,
            (inputs.repository, inputs.pull_request, inputs.head_sha, new_run),
        ).fetchall()
        return self._retire(conn, new_run, rows, actor=actor)

    def _retire(
        self, conn: Connection, new_run: UUID, rows: list[DictRow], *, actor: str
    ) -> tuple[UUID, ...]:
        """Mark runs superseded by ``new_run`` and cancel those not yet past cancellation."""
        for row in rows:
            conn.execute(
                "UPDATE runs SET superseded_by = %s, updated_at = statement_timestamp() "
                "WHERE run_id = %s",
                (new_run, row["run_id"]),
            )
            state = RunState(row["state"])
            if state in CANCELLABLE_STATES:
                self._revoke(
                    conn,
                    row["run_id"],
                    state,
                    RunState.CANCELLED,
                    actor=actor,
                    detail={"reason": "superseded", "superseded_by": str(new_run)},
                )
        return tuple(row["run_id"] for row in rows)

    # Trusted interruptions (no lease) ----------------------------------------------------------

    def cancel(
        self,
        run_id: UUID,
        *,
        actor: str,
        reason: str = "requested",
        decision: HumanDecision | None = None,
    ) -> bool:
        """Cancel a working or reviewable run; return False if it is already ending or ended.

        ``decision``, a reviewer's ``CANCEL_RUN`` on this run, is recorded with the cancellation.
        """
        if decision is not None and (
            decision.action is not HumanDecisionAction.CANCEL_RUN or decision.run_id != run_id
        ):
            raise ValueError("the decision does not cancel this run")
        with self._transaction() as conn:
            state = self._lock_run(conn, run_id)
            if state not in CANCELLABLE_STATES:
                return False
            self._revoke(
                conn, run_id, state, RunState.CANCELLED, actor=actor, detail={"reason": reason}
            )
            if decision is not None:
                run = self._load(conn, run_id)
                self._insert_decision(conn, decision, run.inputs.repository, None)
            return True

    def close_pull_request(
        self, repository: str, pull_request: int, *, actor: str
    ) -> tuple[UUID, ...]:
        """Cancel every working or reviewable run of a closed pull request; return them."""
        with self._transaction() as conn:
            self._lock_pull_request(conn, repository, pull_request)
            rows = conn.execute(
                """
                SELECT run_id, state FROM runs
                WHERE repository = %s AND pull_request = %s AND state = ANY(%s)
                ORDER BY created_at, run_id
                FOR UPDATE
                """,
                (repository, pull_request, [state.value for state in CANCELLABLE_STATES]),
            ).fetchall()
            for row in rows:
                self._revoke(
                    conn,
                    row["run_id"],
                    RunState(row["state"]),
                    RunState.CANCELLED,
                    actor=actor,
                    detail={"reason": "pull request closed"},
                )
            return tuple(row["run_id"] for row in rows)

    def expire_overdue(self, *, actor: str) -> list[UUID]:
        """Time out every working run whose deadline has passed."""
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT run_id, state FROM runs
                WHERE deadline_at <= statement_timestamp() AND state = ANY(%s)
                ORDER BY run_id
                FOR UPDATE
                """,
                ([state.value for state in WORKING_STATES],),
            ).fetchall()
            for row in rows:
                self._revoke(
                    conn,
                    row["run_id"],
                    RunState(row["state"]),
                    RunState.TIMED_OUT,
                    actor=actor,
                    detail={"reason": "run deadline passed"},
                )
            return [row["run_id"] for row in rows]

    def _revoke(
        self,
        conn: Connection,
        run_id: UUID,
        current: RunState,
        target: RunState,
        *,
        actor: str,
        detail: Mapping[str, Any],
    ) -> None:
        """Interrupt a run from outside its lease and revoke whoever holds the lease."""
        check_transition(current, target)
        token = conn.execute(
            """
            UPDATE run_jobs
            SET fencing_token = fencing_token + 1, attempts = 0, last_error = NULL,
                available_at = statement_timestamp(), lease_owner = NULL,
                lease_acquired_at = NULL, heartbeat_at = NULL, lease_expires_at = NULL
            WHERE run_id = %s
            RETURNING fencing_token
            """,
            (run_id,),
        ).fetchone()
        token = _required(token, "a non-terminal run always has a job")
        conn.execute(
            "UPDATE runs SET state = %s, updated_at = statement_timestamp() WHERE run_id = %s",
            (target.value, run_id),
        )
        self._log(
            conn, run_id, current, target, actor=actor, token=token["fencing_token"], detail=detail
        )

    # Worker operations (fenced) ----------------------------------------------------------------

    def claim(self, worker_id: str, *, lease: timedelta) -> Claim | None:
        """Lease the next available run, or return None if there is none."""
        with self._transaction() as conn:
            job = conn.execute(
                """
                WITH candidate AS (
                    SELECT run_id FROM run_jobs
                    WHERE available_at <= statement_timestamp()
                      AND (lease_expires_at IS NULL OR lease_expires_at <= statement_timestamp())
                    ORDER BY available_at, run_id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE run_jobs AS job
                SET fencing_token = job.fencing_token + 1,
                    attempts = job.attempts + 1,
                    lease_owner = %(worker)s,
                    lease_acquired_at = statement_timestamp(),
                    heartbeat_at = statement_timestamp(),
                    lease_expires_at = statement_timestamp() + %(lease)s
                FROM candidate
                WHERE job.run_id = candidate.run_id
                RETURNING job.run_id, job.fencing_token, job.attempts, job.max_attempts
                """,
                {"worker": worker_id, "lease": lease},
            ).fetchone()
            if job is None:
                return None
            run = _required(
                conn.execute(
                    "SELECT state FROM runs WHERE run_id = %s", (job["run_id"],)
                ).fetchone(),
                "every job belongs to a run",
            )
            return Claim(
                run_id=job["run_id"],
                worker_id=worker_id,
                fencing_token=job["fencing_token"],
                state=RunState(run["state"]),
                attempt=job["attempts"],
                max_attempts=job["max_attempts"],
            )

    def heartbeat(self, claim: Claim, *, lease: timedelta) -> None:
        """Extend a live lease. An expired or revoked lease cannot be revived."""
        with self._transaction() as conn:
            extended = conn.execute(
                """
                UPDATE run_jobs
                SET heartbeat_at = statement_timestamp(),
                    lease_expires_at = statement_timestamp() + %(lease)s
                WHERE run_id = %(run)s AND lease_owner = %(worker)s
                  AND fencing_token = %(token)s AND lease_expires_at > statement_timestamp()
                """,
                {
                    "lease": lease,
                    "run": claim.run_id,
                    "worker": claim.worker_id,
                    "token": claim.fencing_token,
                },
            )
            if extended.rowcount != 1:
                raise LeaseLostError(f"lease on run {claim.run_id} was lost")

    def advance(
        self,
        claim: Claim,
        target: RunState,
        *,
        detail: Mapping[str, Any] | None = None,
        error_code: ErrorCode | None = None,
        not_executed_reason: NotExecutedReason | None = None,
        isolation_id: str | None = None,
        effects: Effects | None = None,
    ) -> Claim:
        """Make a legal transition under the lease, together with ``effects``, atomically.

        The holder keeps its lease and starts the next stage as attempt 1. Use
        :meth:`complete_cleaning` to leave ``CLEANING``.
        """
        if target in (RunState.COMPLETE, RunState.SUPERSEDED):
            raise ValueError("use complete_cleaning() to end a run after cleanup")
        with self._transaction() as conn:
            current = self._hold(conn, claim)
            check_transition(current, target)
            conn.execute(
                """
                UPDATE runs
                SET state = %(state)s,
                    error_code = COALESCE(%(error)s, error_code),
                    not_executed_reason = %(not_executed)s,
                    isolation_id = COALESCE(%(isolation)s, isolation_id),
                    updated_at = statement_timestamp()
                WHERE run_id = %(run)s
                """,
                {
                    "state": target.value,
                    "error": error_code.value if error_code else None,
                    "not_executed": not_executed_reason.value if not_executed_reason else None,
                    "isolation": isolation_id,
                    "run": claim.run_id,
                },
            )
            self._log(
                conn,
                claim.run_id,
                current,
                target,
                actor=f"worker:{claim.worker_id}",
                token=claim.fencing_token,
                detail=detail or {},
            )
            if effects is not None:
                effects(conn)
            if is_terminal(target):
                conn.execute("DELETE FROM run_jobs WHERE run_id = %s", (claim.run_id,))
            else:
                conn.execute(
                    "UPDATE run_jobs SET attempts = 1, last_error = NULL WHERE run_id = %s",
                    (claim.run_id,),
                )
        return replace(claim, state=target, attempt=1)

    def complete_cleaning(self, claim: Claim, *, detail: Mapping[str, Any]) -> RunState:
        """End a cleaned run: ``SUPERSEDED`` if a newer run replaced it, otherwise ``COMPLETE``."""
        with self._transaction() as conn:
            current = self._hold(conn, claim)
            row = _required(
                conn.execute(
                    "SELECT superseded_by FROM runs WHERE run_id = %s", (claim.run_id,)
                ).fetchone(),
                "a held run exists",
            )
            target = RunState.SUPERSEDED if row["superseded_by"] else RunState.COMPLETE
            check_transition(current, target)
            conn.execute(
                "UPDATE runs SET state = %s, updated_at = statement_timestamp() WHERE run_id = %s",
                (target.value, claim.run_id),
            )
            self._log(
                conn,
                claim.run_id,
                current,
                target,
                actor=f"worker:{claim.worker_id}",
                token=claim.fencing_token,
                detail=detail,
            )
            conn.execute("DELETE FROM run_jobs WHERE run_id = %s", (claim.run_id,))
            return target

    def park(self, claim: Claim, *, until: timedelta) -> None:
        """Release the lease; the run is claimable again after ``until``, with a fresh budget."""
        with self._transaction() as conn:
            self._hold(conn, claim)
            conn.execute(
                """
                UPDATE run_jobs
                SET available_at = statement_timestamp() + %s, attempts = 0, last_error = NULL,
                    lease_owner = NULL, lease_acquired_at = NULL, heartbeat_at = NULL,
                    lease_expires_at = NULL
                WHERE run_id = %s
                """,
                (until, claim.run_id),
            )

    def retry_later(self, claim: Claim, *, error: str, delay: timedelta) -> None:
        """Release the lease after a failed attempt; the stage is retried after ``delay``."""
        with self._transaction() as conn:
            self._hold(conn, claim)
            conn.execute(
                """
                UPDATE run_jobs
                SET available_at = statement_timestamp() + %s, last_error = %s,
                    lease_owner = NULL, lease_acquired_at = NULL, heartbeat_at = NULL,
                    lease_expires_at = NULL
                WHERE run_id = %s
                """,
                (delay, error[:_MAX_ERROR_TEXT], claim.run_id),
            )

    def last_error(self, run_id: UUID) -> str | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT last_error FROM run_jobs WHERE run_id = %s", (run_id,)
            ).fetchone()
            return None if row is None else row["last_error"]

    # Reads -------------------------------------------------------------------------------------

    def get_run(self, run_id: UUID) -> ReviewRun:
        with self._transaction() as conn:
            return self._load(conn, run_id)

    def runs(
        self,
        repositories: Sequence[str],
        *,
        pull_request: int | None = None,
        limit: int = 50,
    ) -> list[ReviewRun]:
        """The latest runs of these repositories (optionally one pull request), newest first."""
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE repository = ANY(%s) AND (%s::integer IS NULL OR pull_request = %s)
                ORDER BY created_at DESC, run_id DESC
                LIMIT %s
                """,
                (list(repositories), pull_request, pull_request, limit),
            ).fetchall()
            return [self._load(conn, row["run_id"]) for row in rows]

    def transitions(self, run_id: UUID) -> list[Transition]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT seq, from_state, to_state, actor, fencing_token, detail, at "
                "FROM run_transitions WHERE run_id = %s ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [
            Transition(
                seq=row["seq"],
                from_state=RunState(row["from_state"]) if row["from_state"] else None,
                to_state=RunState(row["to_state"]),
                actor=row["actor"],
                fencing_token=row["fencing_token"],
                detail=row["detail"],
                at=row["at"],
            )
            for row in rows
        ]

    # Internals ---------------------------------------------------------------------------------

    def _lock_run(self, conn: Connection, run_id: UUID) -> RunState:
        row = conn.execute(
            "SELECT state FROM runs WHERE run_id = %s FOR UPDATE", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(str(run_id))
        conn.execute("SELECT 1 FROM run_jobs WHERE run_id = %s FOR UPDATE", (run_id,))
        return RunState(row["state"])

    def _hold(self, conn: Connection, claim: Claim) -> RunState:
        """Lock the run and verify, in this transaction, that the claim still holds the lease."""
        state = self._lock_run(conn, claim.run_id)
        job = conn.execute(
            """
            SELECT lease_owner = %(worker)s AND fencing_token = %(token)s
                   AND lease_expires_at > statement_timestamp() AS held
            FROM run_jobs WHERE run_id = %(run)s
            """,
            {"worker": claim.worker_id, "token": claim.fencing_token, "run": claim.run_id},
        ).fetchone()
        if job is None or not job["held"]:
            raise LeaseLostError(f"lease on run {claim.run_id} was lost")
        if state is not claim.state:
            raise LeaseLostError(f"run {claim.run_id} is {state}, not {claim.state}")
        return state

    def _log(
        self,
        conn: Connection,
        run_id: UUID,
        current: RunState | None,
        target: RunState,
        *,
        actor: str,
        token: int | None,
        detail: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO run_transitions (run_id, seq, from_state, to_state, actor,
                                         fencing_token, detail, at)
            SELECT %(run)s, COALESCE(MAX(seq), 0) + 1, %(from)s, %(to)s, %(actor)s, %(token)s,
                   %(detail)s, statement_timestamp()
            FROM run_transitions WHERE run_id = %(run)s
            """,
            {
                "run": run_id,
                "from": current.value if current else None,
                "to": target.value,
                "actor": actor,
                "token": token,
                "detail": Jsonb(dict(detail)),
            },
        )

    def _load(self, conn: Connection, run_id: UUID) -> ReviewRun:
        row = conn.execute(
            """
            SELECT r.*, j.lease_owner, j.fencing_token, j.lease_acquired_at, j.heartbeat_at,
                   j.lease_expires_at,
                   j.lease_expires_at > statement_timestamp() AS lease_live
            FROM runs AS r LEFT JOIN run_jobs AS j USING (run_id)
            WHERE r.run_id = %s
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(str(run_id))
        lease = None
        if row["lease_owner"] is not None and row["lease_live"]:
            lease = WorkerLease(
                worker_id=row["lease_owner"],
                fencing_token=row["fencing_token"],
                acquired_at=row["lease_acquired_at"],
                heartbeat_at=row["heartbeat_at"],
                expires_at=row["lease_expires_at"],
            )
        return ReviewRun(
            run_id=row["run_id"],
            inputs=RunInputs.model_validate(row["inputs"]),
            input_digest=row["input_digest"],
            trigger=RunTrigger(row["trigger"]),
            parent_run_id=row["parent_run_id"],
            state=RunState(row["state"]),
            error_code=ErrorCode(row["error_code"]) if row["error_code"] else None,
            not_executed_reason=(
                NotExecutedReason(row["not_executed_reason"])
                if row["not_executed_reason"]
                else None
            ),
            superseded_by=row["superseded_by"],
            lease=lease,
            isolation_id=row["isolation_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
