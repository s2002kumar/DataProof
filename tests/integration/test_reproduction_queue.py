"""The reproduction queue on PostgreSQL: one request per check, leased claims, retries and ends."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest

from control_env import create, execute
from dataproof_control import ReproductionClaim, ReproductionQueue, RunStore

pytestmark = pytest.mark.integration
LEASE = timedelta(minutes=5)


@pytest.fixture
def queue(control_dsn: str) -> ReproductionQueue:
    return ReproductionQueue(control_dsn)


def test_a_check_is_requested_once(store: RunStore, queue: ReproductionQueue) -> None:
    run_id = create(store).run.run_id
    first, created = queue.request(run_id, "commerce.c1", requested_by="maintainer")
    again, created_again = queue.request(run_id, "commerce.c1", requested_by="someone-else")
    assert (created, created_again) == (True, False)
    assert again == first
    assert (first.state, first.requested_by) == ("queued", "maintainer")


def test_a_claimed_job_is_running_until_its_lease_expires(
    store: RunStore, queue: ReproductionQueue, control_dsn: str
) -> None:
    run_id = create(store).run.run_id
    queue.request(run_id, "commerce.c1", requested_by="maintainer")
    claim = queue.claim("w1", lease=LEASE)
    assert claim is not None
    assert (claim.run_id, claim.check_id, claim.attempt) == (run_id, "commerce.c1", 1)
    assert queue.claim("w2", lease=LEASE) is None, "a leased job is not claimed twice"
    assert queue.requests(run_id)[0].state == "running"

    execute(control_dsn, "UPDATE reproduction_jobs SET lease_expires_at = now() - interval '1s'")
    reclaimed = queue.claim("w2", lease=LEASE)
    assert reclaimed is not None
    assert reclaimed.attempt == 2, "a crashed worker's attempt counts"


def test_a_failed_attempt_is_retried_until_the_attempts_are_spent(
    store: RunStore, queue: ReproductionQueue
) -> None:
    run_id = create(store).run.run_id
    queue.request(run_id, "commerce.c1", requested_by="maintainer", max_attempts=2)
    first = queue.claim("w1", lease=LEASE)
    assert first is not None
    queue.fail(first, "Docker was not reachable")
    assert queue.requests(run_id)[0].state == "queued"
    second = queue.claim("w1", lease=LEASE)
    assert second is not None
    queue.fail(second, "Docker was still not reachable")
    (ended,) = queue.requests(run_id)
    assert (ended.state, ended.failure) == ("failed", "Docker was still not reachable")
    assert queue.claim("w1", lease=LEASE) is None


def test_a_request_that_cannot_succeed_ends_at_once(
    store: RunStore, queue: ReproductionQueue
) -> None:
    run_id = create(store).run.run_id
    queue.request(run_id, "commerce.c1", requested_by="maintainer", max_attempts=3)
    claim = queue.claim("w1", lease=LEASE)
    assert claim is not None
    queue.fail(claim, "commerce.c1 did not fail", final=True)
    assert queue.requests(run_id)[0].state == "failed"


def test_a_worker_that_lost_its_lease_cannot_end_the_job(
    store: RunStore, queue: ReproductionQueue
) -> None:
    run_id = create(store).run.run_id
    queue.request(run_id, "commerce.c1", requested_by="maintainer")
    claim = queue.claim("w1", lease=LEASE)
    assert claim is not None
    stale = ReproductionClaim(
        claim.request_id, claim.run_id, claim.check_id, "w-stale", claim.attempt, 2
    )
    queue.fail(stale, "late", final=True)
    assert queue.requests(run_id)[0].state == "running"


def test_concurrent_workers_claim_each_job_once(store: RunStore, queue: ReproductionQueue) -> None:
    run_id = create(store).run.run_id
    for check in ("c1", "c2", "c3"):
        queue.request(run_id, check, requested_by="maintainer")
    barrier = threading.Barrier(6)

    def claim(index: int) -> str | None:
        barrier.wait()
        claimed = queue.claim(f"w{index}", lease=LEASE)
        return claimed.check_id if claimed else None

    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = [c for c in pool.map(claim, range(6)) if c is not None]
    assert sorted(claimed) == ["c1", "c2", "c3"]


def test_requests_are_write_once(
    control_dsn: str, store: RunStore, queue: ReproductionQueue
) -> None:
    queue.request(create(store).run.run_id, "commerce.c1", requested_by="maintainer")
    with (
        psycopg.connect(control_dsn, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="write-once"),
    ):
        conn.execute("UPDATE reproduction_requests SET requested_by = 'someone-else'")
