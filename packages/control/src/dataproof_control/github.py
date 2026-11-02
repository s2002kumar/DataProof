"""What GitHub told the control plane: verified webhook deliveries and pull request heads.

Both are write-once. A delivery is recorded only after its signature was verified, so the table
is an audit of what intake did with every genuine event, including the ones it ignored or
refused. Pull request heads carry the facts that later decisions need, such as who opened the
pull request (ADR-0005, approver rule).
"""

from collections.abc import Iterator, Mapping
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
