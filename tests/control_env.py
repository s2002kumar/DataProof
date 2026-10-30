"""Control-database helpers for run-lifecycle tests.

Time-dependent behaviour (lease expiry, retry backoff, review TTL, run deadlines) is tested by
moving the relevant timestamps into the past rather than by sleeping, except where a test is
specifically about real elapsed time (heartbeats).
"""

from datetime import timedelta
from typing import Any, LiteralString
from uuid import UUID

import psycopg

from dataproof_contracts import RunInputs, RunTrigger
from dataproof_control import CreatedRun, RetryPolicy, RunStore

REPOSITORY = "example-org/commerce-demo"
BASE_SHA = "aa6e427d334b832d952d658cae44f65f99ffaf74"
HEAD_SHA = "8f3ab2a3b2ce14772811945b1f7ea036b769932e"
NEW_HEAD_SHA = "0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d"
CONFIG_DIGEST = "a40af8b9cf5187bb554a30dd4759e233cfaa771e715bbf9f26d6894d8967c7fb"
DATASET_DIGEST = "b4f47bea876de4049bedf0968fda8c055f327938fc5010c990d75bfe9e1a0530"

FAST_RETRY = RetryPolicy(max_attempts=3, base_delay=timedelta(0), max_delay=timedelta(0))
LEASE = timedelta(seconds=30)
RUN_TTL = timedelta(minutes=30)


def run_inputs(
    *,
    head_sha: str = HEAD_SHA,
    pull_request: int | None = 7,
    config_digest: str = CONFIG_DIGEST,
    repository: str = REPOSITORY,
) -> RunInputs:
    return RunInputs(
        repository=repository,
        pull_request=pull_request,
        base_sha=BASE_SHA,
        head_sha=head_sha,
        config_digest=config_digest,
        dataset_digest=DATASET_DIGEST,
        engine_version="0.1.0",
    )


def create(
    store: RunStore,
    inputs: RunInputs | None = None,
    *,
    trigger: RunTrigger = RunTrigger.WEBHOOK,
    parent_run_id: UUID | None = None,
    retry: RetryPolicy = FAST_RETRY,
) -> CreatedRun:
    return store.create_run(
        inputs or run_inputs(),
        trigger,
        actor="test",
        run_ttl=RUN_TTL,
        retry=retry,
        parent_run_id=parent_run_id,
    )


def execute(dsn: str, statement: LiteralString, *params: Any) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(statement, params)


def expire_lease(dsn: str, run_id: UUID) -> None:
    """The lease holder stopped heartbeating long enough ago for its lease to have lapsed."""
    execute(
        dsn,
        "UPDATE run_jobs SET lease_acquired_at = statement_timestamp() - interval '3 seconds', "
        "heartbeat_at = statement_timestamp() - interval '2 seconds', "
        "lease_expires_at = statement_timestamp() - interval '1 second' WHERE run_id = %s",
        run_id,
    )


def make_due(dsn: str, run_id: UUID) -> None:
    """A parked or backed-off job's waiting time has elapsed."""
    execute(
        dsn,
        "UPDATE run_jobs SET available_at = statement_timestamp() - interval '1 second' "
        "WHERE run_id = %s",
        run_id,
    )


def pass_deadline(dsn: str, run_id: UUID) -> None:
    execute(
        dsn,
        "UPDATE runs SET deadline_at = statement_timestamp() - interval '1 second' "
        "WHERE run_id = %s",
        run_id,
    )
