"""The reproduction queue: reviewers' requests, run by workers (migration 0010).

A request is recorded once per check of a run; asking again returns it. A worker claims a job
with ``FOR UPDATE SKIP LOCKED`` and a lease, so a crashed worker's job is claimed again once
the lease expires. A job ends with the ``Reproduction`` it recorded, or, when its attempts are
spent, with the reason it failed.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow, dict_row

_MAX_FAILURE_TEXT = 1000


@dataclass(frozen=True)
class ReproductionRequest:
    request_id: UUID
    run_id: UUID
    check_id: str
    requested_by: str
    requested_at: datetime
    state: str
    """``queued``, ``running``, ``reproduced`` (a ``Reproduction`` was recorded) or ``failed``."""
    reproduction_id: UUID | None
    failure: str | None


@dataclass(frozen=True)
class ReproductionClaim:
    request_id: UUID
    run_id: UUID
    check_id: str
    worker_id: str
    attempt: int
    max_attempts: int


class ReproductionQueue:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection[DictRow]]:
        with (
            psycopg.connect(self._conninfo, autocommit=True, row_factory=dict_row) as conn,
            conn.transaction(),
        ):
            yield conn

    def request(
        self, run_id: UUID, check_id: str, *, requested_by: str, max_attempts: int = 2
    ) -> tuple[ReproductionRequest, bool]:
        """Queue a reproduction of one check; the request and whether it is new."""
        with self._transaction() as conn:
            request_id = uuid4()
            inserted = conn.execute(
                "INSERT INTO reproduction_requests (request_id, run_id, check_id, requested_by) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (run_id, check_id) DO NOTHING "
                "RETURNING request_id",
                (request_id, run_id, check_id, requested_by),
            ).fetchone()
            if inserted is not None:
                conn.execute(
                    "INSERT INTO reproduction_jobs (request_id, max_attempts) VALUES (%s, %s)",
                    (request_id, max_attempts),
                )
        (found,) = [r for r in self.requests(run_id) if r.check_id == check_id]
        return found, inserted is not None

    def requests(self, run_id: UUID) -> list[ReproductionRequest]:
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT r.request_id, r.run_id, r.check_id, r.requested_by, r.requested_at,
                       j.finished_at, j.reproduction_id, j.failure,
                       j.lease_expires_at > statement_timestamp() AS leased
                FROM reproduction_requests AS r JOIN reproduction_jobs AS j USING (request_id)
                WHERE r.run_id = %s
                ORDER BY r.requested_at, r.request_id
                """,
                (run_id,),
            ).fetchall()
        return [
            ReproductionRequest(
                request_id=row["request_id"],
                run_id=row["run_id"],
                check_id=row["check_id"],
                requested_by=row["requested_by"],
                requested_at=row["requested_at"],
                state=(
                    "reproduced"
                    if row["reproduction_id"]
                    else "failed"
                    if row["failure"]
                    else "running"
                    if row["leased"]
                    else "queued"
                ),
                reproduction_id=row["reproduction_id"],
                failure=row["failure"],
            )
            for row in rows
        ]

    def claim(self, worker_id: str, *, lease: timedelta) -> ReproductionClaim | None:
        with self._transaction() as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT request_id FROM reproduction_jobs
                    WHERE finished_at IS NULL AND attempts < max_attempts
                      AND (lease_expires_at IS NULL OR lease_expires_at <= statement_timestamp())
                    ORDER BY request_id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE reproduction_jobs AS job
                SET attempts = job.attempts + 1, lease_owner = %(worker)s,
                    lease_expires_at = statement_timestamp() + %(lease)s
                FROM candidate, reproduction_requests AS r
                WHERE job.request_id = candidate.request_id AND r.request_id = job.request_id
                RETURNING job.request_id, r.run_id, r.check_id, job.attempts, job.max_attempts
                """,
                {"worker": worker_id, "lease": lease},
            ).fetchone()
        if row is None:
            return None
        return ReproductionClaim(
            request_id=row["request_id"],
            run_id=row["run_id"],
            check_id=row["check_id"],
            worker_id=worker_id,
            attempt=row["attempts"],
            max_attempts=row["max_attempts"],
        )

    def finish(self, claim: ReproductionClaim, reproduction_id: UUID) -> None:
        self._end(claim, reproduction_id=reproduction_id, failure=None)

    def fail(self, claim: ReproductionClaim, failure: str, *, final: bool = False) -> None:
        """Record a failed attempt; the job ends once its attempts are spent (or ``final``)."""
        if final or claim.attempt >= claim.max_attempts:
            self._end(claim, reproduction_id=None, failure=failure[:_MAX_FAILURE_TEXT])
            return
        with self._transaction() as conn:
            conn.execute(
                "UPDATE reproduction_jobs SET lease_owner = NULL, lease_expires_at = NULL "
                "WHERE request_id = %s AND lease_owner = %s AND finished_at IS NULL",
                (claim.request_id, claim.worker_id),
            )

    def _end(
        self, claim: ReproductionClaim, *, reproduction_id: UUID | None, failure: str | None
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE reproduction_jobs
                SET finished_at = statement_timestamp(), reproduction_id = %s, failure = %s,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE request_id = %s AND lease_owner = %s AND finished_at IS NULL
                """,
                (reproduction_id, failure, claim.request_id, claim.worker_id),
            )
