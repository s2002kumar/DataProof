"""Values read from real PostgreSQL keep their exact meaning through the contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from dataproof_contracts import (
    CheckOutcome,
    CheckResult,
    DecimalValue,
    Mismatch,
    NullValue,
    TextValue,
    TimestampValue,
    typed_value,
)

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[tuple[Any, ...]]


def _row(pg: Connection, sql: str) -> tuple[Any, ...]:
    row = pg.execute(sql).fetchone()
    assert row is not None
    return row


def test_numeric_amounts_round_trip_exactly(pg: Connection) -> None:
    expected, doubled, total = _row(
        pg,
        "SELECT 49.99::numeric(18,2), (49.99::numeric(18,2) * 2)::numeric(18,2),"
        " 0.10::numeric(18,2) + 0.20::numeric(18,2)",
    )
    assert (expected, doubled, total) == (Decimal("49.99"), Decimal("99.98"), Decimal("0.30"))

    mismatch = Mismatch(
        key=(typed_value(1002),),
        column="amount",
        expected=typed_value(expected),
        observed=typed_value(doubled),
    )
    payload = mismatch.model_dump_json()
    assert '"value":"49.99"' in payload
    assert '"value":"99.98"' in payload
    assert Mismatch.model_validate_json(payload) == mismatch


def test_timestamptz_instants_survive_a_non_utc_session(pg: Connection) -> None:
    # Toronto switches from EST to EDT at 07:00 UTC on 2026-03-08.
    pg.execute("SET TIME ZONE 'America/Toronto'")
    before, after = _row(
        pg, "SELECT '2026-03-08 06:30:00+00'::timestamptz, '2026-03-08 07:30:00+00'::timestamptz"
    )
    assert before.utcoffset() == timedelta(hours=-5)
    assert after.utcoffset() == timedelta(hours=-4)
    # psycopg returns both in one ZoneInfo, so Python subtracts wall clocks (01:30 -> 03:30).
    assert after - before == timedelta(hours=2)

    first, second = typed_value(before), typed_value(after)
    assert isinstance(first, TimestampValue)
    assert isinstance(second, TimestampValue)
    assert second.value - first.value == timedelta(hours=1)
    restored = TimestampValue.model_validate_json(first.model_dump_json())
    assert restored.value == datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    assert restored.model_dump_json() == first.model_dump_json()


def test_null_and_empty_string_stay_distinct(pg: Connection) -> None:
    null, empty = _row(pg, "SELECT NULL::text, ''::text")
    assert typed_value(null) == NullValue()
    assert typed_value(empty) == TextValue(value="")
    assert typed_value(null) != typed_value(empty)


def test_keyed_comparison_produces_a_valid_failing_check(pg: Connection) -> None:
    pg.execute(
        "CREATE TEMP TABLE reference_events (event_id bigint PRIMARY KEY, amount numeric(18,2))"
    )
    pg.execute(
        "CREATE TEMP TABLE candidate_ledger"
        " (source_event_id bigint PRIMARY KEY, amount numeric(18,2))"
    )
    pg.execute("INSERT INTO reference_events VALUES (1001, 120.00), (1002, 49.99)")
    pg.execute("INSERT INTO candidate_ledger VALUES (1001, 120.00), (1002, 99.98)")
    rows = pg.execute(
        "SELECT r.event_id, r.amount, c.amount FROM reference_events r"
        " FULL JOIN candidate_ledger c ON c.source_event_id = r.event_id"
        " WHERE r.amount IS DISTINCT FROM c.amount ORDER BY r.event_id"
    ).fetchall()

    result = CheckResult(
        check_id="c1.amount",
        contract_id="commerce.c1_event_preservation",
        contract_version=1,
        required=True,
        outcome=CheckOutcome.FAIL,
        mismatch_count=len(rows),
        mismatch_examples=tuple(
            Mismatch(
                key=(typed_value(key),),
                column="amount",
                expected=typed_value(ref),
                observed=typed_value(cand),
            )
            for key, ref, cand in rows
        ),
        evidence_ids=("ev-integration-query",),
        dataset_digest="0" * 64,
    )
    (mismatch,) = result.mismatch_examples
    assert mismatch.expected == DecimalValue(value=Decimal("49.99"))
    assert mismatch.observed == DecimalValue(value=Decimal("99.98"))
    assert CheckResult.model_validate_json(result.model_dump_json()) == result
