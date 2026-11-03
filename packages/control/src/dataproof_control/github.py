"""What GitHub told the control plane: verified webhook deliveries and pull request heads.

All are write-once. A delivery is recorded only after its signature was verified, so the table
is an audit of what intake did with every genuine event, including the ones it ignored or
refused. Pull request heads carry the facts that later decisions need, such as who opened the
pull request (ADR-0005, approver rule). Check runs and their publications are the outbox from
which each run's status reaches GitHub; omissions record the runs left to a newer run on their
commit.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb


class DeliveryOutcome(StrEnum):
    RUN_CREATED = "RUN_CREATED"
    RUN_EXISTS = "RUN_EXISTS"
    RUNS_CANCELLED = "RUNS_CANCELLED"
    IGNORED = "IGNORED"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    event: str
    outcome: DeliveryOutcome
    action: str | None = None
    repository: str | None = None
    run_id: UUID | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckRun:
    """The GitHub check run that reports one run, on its exact head commit."""

    run_id: UUID
    repository: str
    head_sha: str
    check_run_id: int


@dataclass(frozen=True)
class PullRequestHead:
    repository: str
    pull_request: int
    head_sha: str
    head_repository: str
    author: str
    installation_id: int


class GitHubRecords:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[DictRow]]:
        with psycopg.connect(self._conninfo, autocommit=True, row_factory=dict_row) as conn:
            yield conn

    def record_delivery(self, delivery: Delivery) -> bool:
        """Record a delivery once. False if it was already recorded, which then stands."""
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO github_deliveries (delivery_id, event, action, repository, outcome,
                                               run_id, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (delivery_id) DO NOTHING
                """,
                (
                    delivery.delivery_id,
                    delivery.event,
                    delivery.action,
                    delivery.repository,
                    delivery.outcome.value,
                    delivery.run_id,
                    Jsonb(dict(delivery.detail)),
                ),
            )
            return inserted.rowcount == 1

    def delivery(self, delivery_id: str) -> Delivery | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM github_deliveries WHERE delivery_id = %s", (delivery_id,)
            ).fetchone()
        if row is None:
            return None
        return Delivery(
            delivery_id=row["delivery_id"],
            event=row["event"],
            outcome=DeliveryOutcome(row["outcome"]),
            action=row["action"],
            repository=row["repository"],
            run_id=row["run_id"],
            detail=row["detail"],
        )

    def record_head(self, head: PullRequestHead) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO github_pull_request_heads (repository, pull_request, head_sha,
                                                       head_repository, author, installation_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (repository, pull_request, head_sha) DO NOTHING
                """,
                (
                    head.repository,
                    head.pull_request,
                    head.head_sha,
                    head.head_repository,
                    head.author,
                    head.installation_id,
                ),
            )

    def head(self, repository: str, pull_request: int, head_sha: str) -> PullRequestHead | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT repository, pull_request, head_sha, head_repository, author, "
                "installation_id FROM github_pull_request_heads "
                "WHERE repository = %s AND pull_request = %s AND head_sha = %s",
                (repository, pull_request, head_sha),
            ).fetchone()
        return PullRequestHead(**row) if row else None

    # Check runs --------------------------------------------------------------------------------

    def check_run(self, run_id: UUID) -> CheckRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id, repository, head_sha, check_run_id FROM github_check_runs "
                "WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        return CheckRun(**row) if row else None

    def record_check_run(self, check: CheckRun) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO github_check_runs (run_id, repository, head_sha, check_run_id) "
                "VALUES (%s, %s, %s, %s)",
                (check.run_id, check.repository, check.head_sha, check.check_run_id),
            )

    def publications(self, run_id: UUID) -> dict[str, str | None]:
        """What has been published for a run: status to conclusion (None until completed)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, conclusion FROM github_check_publications WHERE run_id = %s",
                (run_id,),
            ).fetchall()
        return {row["status"]: row["conclusion"] for row in rows}

    def record_publication(self, run_id: UUID, status: str, conclusion: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO github_check_publications (run_id, status, conclusion) "
                "VALUES (%s, %s, %s) ON CONFLICT (run_id, status) DO NOTHING",
                (run_id, status, conclusion),
            )

    def unpublished(self, repositories: Sequence[str], limit: int) -> list[UUID]:
        """Pull request runs of these repositories whose completion has not reached GitHub and
        that were not left to a newer run, least recently changed first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id FROM runs AS r
                WHERE r.pull_request IS NOT NULL AND r.repository = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM github_check_publications AS p
                      WHERE p.run_id = r.run_id AND p.status = 'completed'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM github_check_omissions AS o WHERE o.run_id = r.run_id
                  )
                ORDER BY r.updated_at, r.run_id
                LIMIT %s
                """,
                (list(repositories), limit),
            ).fetchall()
        return [row["run_id"] for row in rows]

    def newer_run(self, run_id: UUID) -> UUID | None:
        """The newest pull request run created after this one on the same commit, if any."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT n.run_id FROM runs AS r
                JOIN runs AS n ON n.repository = r.repository AND n.head_sha = r.head_sha
                WHERE r.run_id = %s AND n.pull_request IS NOT NULL
                  AND (n.created_at, n.run_id) > (r.created_at, r.run_id)
                ORDER BY n.created_at DESC, n.run_id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return row["run_id"] if row else None

    def record_omission(self, run_id: UUID, newer_run_id: UUID) -> None:
        """Record that the run gets no check run because ``newer_run_id`` reports instead."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO github_check_omissions (run_id, newer_run_id) VALUES (%s, %s) "
                "ON CONFLICT (run_id) DO NOTHING",
                (run_id, newer_run_id),
            )

    def omission(self, run_id: UUID) -> UUID | None:
        """The newer run recorded when this run was left without a check run, if it was."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT newer_run_id FROM github_check_omissions WHERE run_id = %s", (run_id,)
            ).fetchone()
        return row["newer_run_id"] if row else None

    @contextmanager
    def publishing(self, repository: str, head_sha: str) -> Iterator[bool]:
        """Hold the right to publish check runs on a commit; yields False if another publisher
        has it. Held for the duration of the GitHub calls, so a check run is never created twice
        and the check runs on one commit are created in the order of their runs."""
        with self._connect() as conn:
            key = f"github-check:{repository}@{head_sha}"
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS held", (key,)
            ).fetchone()
            held = bool(row and row["held"])
            try:
                yield held
            finally:
                if held:
                    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
