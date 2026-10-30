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

from collections.abc import Callable, Iterator, Mapping
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
    NotExecutedReason,
    ReviewRun,
    RunInputs,
    RunState,
    RunTrigger,
    WorkerLease,
)
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
#: decision derives from an earlier run never supersede anything.
_SUPERSEDING_TRIGGERS = frozenset({RunTrigger.WEBHOOK, RunTrigger.MANUAL})


def _required(row: DictRow | None, what: str) -> DictRow:
    if row is None:
        raise RuntimeError(f"control database invariant violated: {what}")
    return row


class LeaseLostError(RuntimeError):
    """The caller no longer holds the run's lease and must stop without writing."""


class RunNotFoundError(LookupError):
    pass


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
            if inputs.pull_request is not None:
                # Serialize run creation per pull request so that the later head always wins.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{inputs.repository}#{inputs.pull_request}",),
                )
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

    def cancel(self, run_id: UUID, *, actor: str, reason: str = "requested") -> bool:
        """Cancel a working or reviewable run; return False if it is already ending or ended."""
        with self._transaction() as conn:
            state = self._lock_run(conn, run_id)
            if state not in CANCELLABLE_STATES:
                return False
            self._revoke(
                conn, run_id, state, RunState.CANCELLED, actor=actor, detail={"reason": reason}
            )
            return True

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
