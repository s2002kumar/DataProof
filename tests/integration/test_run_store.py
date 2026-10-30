"""The control store on real PostgreSQL: schema, idempotency, claims, fencing, supersession."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from psycopg import sql

from control_env import (
    LEASE,
    NEW_HEAD_SHA,
    create,
    execute,
    expire_lease,
    pass_deadline,
    run_inputs,
)
from dataproof_contracts import NotExecutedReason, ReviewRun, RunState, RunTrigger
from dataproof_control import (
    TRANSITIONS,
    Claim,
    IllegalTransitionError,
    LeaseLostError,
    MigrationDriftError,
    RunStore,
    migrate,
    migrations,
)

pytestmark = pytest.mark.integration
S = RunState


def _race[T](workers: int, action: Callable[[int], T]) -> list[T]:
    """Release ``workers`` threads at the same instant and collect their results."""
    barrier = threading.Barrier(workers)

    def run(index: int) -> T:
        barrier.wait()
        return action(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, range(workers)))


def _claim(store: RunStore, worker: str = "w1") -> Claim:
    claim = store.claim(worker, lease=LEASE)
    assert claim is not None
    return claim


# Schema ----------------------------------------------------------------------------------------


def test_migrations_are_idempotent(control_dsn: str) -> None:
    with psycopg.connect(control_dsn) as connection:
        assert migrate(connection) == []


def test_changed_applied_migration_is_refused(control_dsn: str) -> None:
    execute(control_dsn, "UPDATE control_schema_migrations SET sha256 = 'tampered'")
    with (
        psycopg.connect(control_dsn) as connection,
        pytest.raises(MigrationDriftError, match="changed after it was applied"),
    ):
        migrate(connection)


def test_migrations_are_numbered_without_gaps() -> None:
    assert [m.version for m in migrations()] == list(range(1, len(migrations()) + 1))


def test_database_transition_table_matches_the_lifecycle(control_dsn: str) -> None:
    with psycopg.connect(control_dsn) as connection:
        rows = connection.execute("SELECT from_state, to_state FROM run_state_transitions")
        in_database = {(S(a), S(b)) for a, b in rows.fetchall()}
        terminal = connection.execute("SELECT state FROM run_states WHERE terminal").fetchall()
    in_code = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
    assert in_database == in_code
    assert {S(row[0]) for row in terminal} == {S.COMPLETE, S.SUPERSEDED, S.NOT_EXECUTED}


def test_database_rejects_an_illegal_transition_even_from_outside_the_store(
    store: RunStore, control_dsn: str
) -> None:
    run = create(store).run
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        execute(
            control_dsn,
            "INSERT INTO run_transitions (run_id, seq, from_state, to_state, actor, at) "
            "VALUES (%s, 2, 'QUEUED', 'COMPLETE', 'intruder', statement_timestamp())",
            run.run_id,
        )


@pytest.mark.parametrize(
    "statement",
    ["UPDATE run_transitions SET actor = 'rewritten'", "DELETE FROM run_transitions"],
)
def test_transition_log_is_append_only(store: RunStore, control_dsn: str, statement: str) -> None:
    create(store)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        execute(control_dsn, statement)


# Idempotent creation ---------------------------------------------------------------------------


def test_creating_the_same_inputs_twice_returns_the_same_run(store: RunStore) -> None:
    first = create(store)
    again = create(store)
    assert first.created
    assert not again.created
    assert again.run.run_id == first.run.run_id
    assert [t.to_state for t in store.transitions(first.run.run_id)] == [S.QUEUED]


def test_concurrent_creation_of_the_same_inputs_creates_one_run(store: RunStore) -> None:
    results = _race(8, lambda _: create(store))
    assert sum(result.created for result in results) == 1
    assert len({result.run.run_id for result in results}) == 1


def test_stored_run_is_a_valid_review_run(store: RunStore) -> None:
    run = create(store).run
    assert isinstance(run, ReviewRun)
    assert run.state is S.QUEUED
    assert run.input_digest == run_inputs().digest()
    assert run.lease is None
    _claim(store)
    leased = store.get_run(run.run_id)
    assert leased.lease is not None
    assert leased.lease.worker_id == "w1"
    assert leased.lease.fencing_token == 1


def test_denormalized_columns_must_agree_with_pinned_inputs(
    store: RunStore, control_dsn: str
) -> None:
    run = create(store).run
    with pytest.raises(psycopg.errors.CheckViolation):
        execute(
            control_dsn, "UPDATE runs SET head_sha = %s WHERE run_id = %s", NEW_HEAD_SHA, run.run_id
        )


def test_derived_runs_require_a_parent(store: RunStore) -> None:
    parent = create(store).run
    approved = run_inputs(config_digest="c" * 64)
    with pytest.raises(psycopg.errors.CheckViolation):
        create(store, approved, trigger=RunTrigger.BINDING_APPROVAL)
    child = create(
        store, approved, trigger=RunTrigger.BINDING_APPROVAL, parent_run_id=parent.run_id
    )
    assert child.run.parent_run_id == parent.run_id


# Claims and leases -----------------------------------------------------------------------------


def test_two_workers_racing_for_one_job_exactly_one_wins(store: RunStore) -> None:
    run = create(store).run
    claims = _race(2, lambda i: store.claim(f"w{i}", lease=LEASE))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].run_id == run.run_id
    assert winners[0].fencing_token == 1
    assert winners[0].attempt == 1


def test_many_workers_claim_each_job_exactly_once(store: RunStore) -> None:
    created = {create(store, run_inputs(pull_request=n)).run.run_id for n in range(1, 13)}
    claims = _race(16, lambda i: store.claim(f"w{i}", lease=LEASE))
    claimed = [claim.run_id for claim in claims if claim is not None]
    assert sorted(claimed) == sorted(created)


def test_a_leased_job_cannot_be_claimed_again(store: RunStore) -> None:
    create(store)
    _claim(store, "w1")
    assert store.claim("w2", lease=LEASE) is None


def test_heartbeat_extends_a_live_lease(store: RunStore) -> None:
    run = create(store).run
    claim = _claim(store)
    before = store.get_run(run.run_id).lease
    store.heartbeat(claim, lease=LEASE * 2)
    after = store.get_run(run.run_id).lease
    assert before is not None
    assert after is not None
    assert after.expires_at > before.expires_at


def test_expired_lease_is_reclaimed_and_the_stale_worker_is_fenced_out(
    store: RunStore, control_dsn: str
) -> None:
    run = create(store).run
    stale = _claim(store, "w1")
    stale = store.advance(stale, S.FETCHING)
    expire_lease(control_dsn, run.run_id)

    fresh = _claim(store, "w2")
    assert fresh.fencing_token == stale.fencing_token + 1
    assert fresh.state is S.FETCHING
    assert fresh.attempt == 2  # the stale worker's attempt at FETCHING counts

    with pytest.raises(LeaseLostError):
        store.advance(stale, S.ANALYZING)
    with pytest.raises(LeaseLostError):
        store.heartbeat(stale, lease=LEASE)
    with pytest.raises(LeaseLostError):
        store.retry_later(stale, error="late", delay=LEASE)

    store.advance(fresh, S.ANALYZING)
    log = store.transitions(run.run_id)
    assert [(t.to_state, t.fencing_token) for t in log] == [
        (S.QUEUED, None),
        (S.FETCHING, 1),
        (S.ANALYZING, 2),
    ]


def test_an_expired_lease_cannot_be_revived_by_its_holder(
    store: RunStore, control_dsn: str
) -> None:
    run = create(store).run
    claim = _claim(store)
    expire_lease(control_dsn, run.run_id)
    with pytest.raises(LeaseLostError):
        store.heartbeat(claim, lease=LEASE)


def test_illegal_transition_is_rejected_and_nothing_is_written(store: RunStore) -> None:
    run = create(store).run
    claim = _claim(store)
    with pytest.raises(IllegalTransitionError):
        store.advance(claim, S.MIGRATING)
    assert store.get_run(run.run_id).state is S.QUEUED
    assert len(store.transitions(run.run_id)) == 1


def test_effects_commit_with_the_transition_or_not_at_all(
    store: RunStore, control_dsn: str
) -> None:
    run = create(store).run
    claim = store.advance(_claim(store), S.FETCHING)
    execute(control_dsn, "CREATE TABLE stage_results (run_id uuid, note text)")

    def record(note: str) -> Callable[[Any], None]:
        def effects(conn: Any) -> None:
            conn.execute("INSERT INTO stage_results VALUES (%s, %s)", (run.run_id, note))

        return effects

    def fail(conn: Any) -> None:
        record("doomed")(conn)
        raise RuntimeError("evidence write failed")

    with pytest.raises(RuntimeError, match="evidence write failed"):
        store.advance(claim, S.ANALYZING, effects=fail)
    assert store.get_run(run.run_id).state is S.FETCHING

    store.advance(claim, S.ANALYZING, effects=record("fetched"))
    with psycopg.connect(control_dsn) as conn:
        notes = conn.execute("SELECT note FROM stage_results").fetchall()
    assert notes == [("fetched",)]


def test_not_executed_requires_a_reason_and_ends_the_job(store: RunStore, control_dsn: str) -> None:
    run = create(store).run
    claim = _claim(store)
    for state in (S.FETCHING, S.ANALYZING):
        claim = store.advance(claim, state)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.advance(claim, S.NOT_EXECUTED)
    store.advance(
        claim,
        S.NOT_EXECUTED,
        not_executed_reason=NotExecutedReason.DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL,
    )
    ended = store.get_run(run.run_id)
    assert ended.state is S.NOT_EXECUTED
    assert store.claim("w2", lease=LEASE) is None


# Cancellation, timeout and supersession --------------------------------------------------------


def test_cancellation_revokes_the_lease_and_leads_to_cleanup(store: RunStore) -> None:
    run = create(store).run
    holder = store.advance(_claim(store, "w1"), S.FETCHING)

    assert store.cancel(run.run_id, actor="user:alice")
    with pytest.raises(LeaseLostError):
        store.advance(holder, S.ANALYZING)

    cleaner = _claim(store, "w2")
    assert cleaner.state is S.CANCELLED
    assert cleaner.attempt == 1
    cleaner = store.advance(cleaner, S.CLEANING)
    assert store.complete_cleaning(cleaner, detail={"cleanup": "SUCCEEDED"}) is S.COMPLETE
    log = store.transitions(run.run_id)
    assert [t.to_state for t in log] == [S.QUEUED, S.FETCHING, S.CANCELLED, S.CLEANING, S.COMPLETE]
    assert log[2].actor == "user:alice"
    assert log[2].detail == {"reason": "requested"}
    assert store.cancel(run.run_id, actor="user:alice") is False


def test_cleaning_runs_cannot_be_cancelled(store: RunStore) -> None:
    run = create(store).run
    store.cancel(run.run_id, actor="user:alice")
    store.advance(_claim(store), S.CLEANING)
    assert store.cancel(run.run_id, actor="user:alice") is False


def test_overdue_runs_time_out(store: RunStore, control_dsn: str) -> None:
    overdue = create(store).run
    on_time = create(store, run_inputs(pull_request=8)).run
    pass_deadline(control_dsn, overdue.run_id)
    assert store.expire_overdue(actor="reconciler") == [overdue.run_id]
    assert store.get_run(overdue.run_id).state is S.TIMED_OUT
    assert store.get_run(on_time.run_id).state is S.QUEUED


def test_runs_in_review_or_cleanup_do_not_time_out(store: RunStore, control_dsn: str) -> None:
    run = create(store).run
    store.cancel(run.run_id, actor="user:alice")
    pass_deadline(control_dsn, run.run_id)
    assert store.expire_overdue(actor="reconciler") == []


def test_a_new_head_supersedes_open_runs_of_the_same_pull_request(store: RunStore) -> None:
    old = create(store).run
    other_pr = create(store, run_inputs(pull_request=8)).run
    holder = store.advance(_claim(store, "w1"), S.FETCHING)
    assert holder.run_id == old.run_id

    new = create(store, run_inputs(head_sha=NEW_HEAD_SHA))
    assert new.superseded == (old.run_id,)
    superseded = store.get_run(old.run_id)
    assert superseded.state is S.CANCELLED
    assert superseded.superseded_by == new.run.run_id
    assert store.get_run(other_pr.run_id).superseded_by is None
    with pytest.raises(LeaseLostError):
        store.advance(holder, S.ANALYZING)

    cleaner = store.claim("w2", lease=LEASE)
    while cleaner is not None and cleaner.run_id != old.run_id:
        cleaner = store.claim("w2", lease=LEASE)
    assert cleaner is not None
    cleaner = store.advance(cleaner, S.CLEANING)
    assert store.complete_cleaning(cleaner, detail={}) is S.SUPERSEDED
    assert store.get_run(old.run_id).state is S.SUPERSEDED


def test_a_run_already_cleaning_still_ends_superseded(store: RunStore) -> None:
    old = create(store).run
    store.cancel(old.run_id, actor="user:alice")
    cleaner = store.advance(_claim(store), S.CLEANING)
    new = create(store, run_inputs(head_sha=NEW_HEAD_SHA)).run
    assert store.get_run(old.run_id).superseded_by == new.run_id
    assert store.complete_cleaning(cleaner, detail={}) is S.SUPERSEDED


def test_binding_approval_on_the_same_head_supersedes_nothing(store: RunStore) -> None:
    original = create(store).run
    approved = create(
        store,
        run_inputs(config_digest="c" * 64),
        trigger=RunTrigger.BINDING_APPROVAL,
        parent_run_id=original.run_id,
    )
    assert approved.superseded == ()
    assert store.get_run(original.run_id).state is S.QUEUED


def test_concurrent_heads_on_one_pull_request_leave_exactly_one_open_run(
    store: RunStore, control_dsn: str
) -> None:
    heads = [f"{n:040x}" for n in range(1, 7)]
    _race(len(heads), lambda i: create(store, run_inputs(head_sha=heads[i])))
    with psycopg.connect(control_dsn) as conn:
        open_runs = conn.execute(
            "SELECT count(*) FROM runs WHERE superseded_by IS NULL AND state = 'QUEUED'"
        ).fetchone()
    assert open_runs == (1,)


@pytest.mark.parametrize(
    "table", ["run_sources", "run_steps", "run_checks", "run_evidence", "run_reports"]
)
def test_run_results_are_write_once(control_dsn: str, table: str) -> None:
    with (
        psycopg.connect(control_dsn, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="write-once"),
    ):
        conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
