"""PR 3 exit criteria for the commerce migrations, against real PostgreSQL.

(a) Detection: change set ``ledger-backfill-07`` executes successfully and keeps one ledger row
    per source event with identical keys, yet the C1 per-event comparison fails, with expected
    49.99 and observed 99.98 for refund event 1002.
(b) Clean control: after change set ``ledger-backfill-01`` the same comparison passes with zero
    mismatches. Without (b), a comparison that always fails would satisfy (a).

Reference and candidate are separate databases built from the same baseline, as in a real run.
The C1 comparison here is hand-written; the general verifier replaces it in a later package.
"""

from collections import Counter
from decimal import Decimal
from typing import Any

import pytest

from commerce_env import (
    JOIN_CHANGE,
    SAFE_CHANGE,
    DatabaseFactory,
    baseline,
    candidate,
    fetch,
    migrate,
    scalar,
)
from commerce_scenario import PUBLISHED_DIGEST, build_fixture
from dataproof_contracts import CheckOutcome, CheckResult, Mismatch, typed_value

pytestmark = pytest.mark.integration

C1_COLUMNS = ("order_id", "kind", "amount", "occurred_at")

Row = tuple[Any, ...]


@pytest.fixture
def reference(new_database: DatabaseFactory) -> str:
    return baseline(new_database)


def reference_events(dsn: str) -> dict[int, Row]:
    query = (
        "SELECT event_id, order_id, event_kind::text, amount, occurred_at"
        " FROM legacy_payment_events"
    )
    return {row[0]: row[1:] for row in fetch(dsn, query)}


def candidate_ledger(dsn: str) -> dict[int, Row]:
    query = (
        "SELECT source_event_id, order_id, entry_kind::text, amount, occurred_at"
        " FROM ledger_entries"
    )
    return {row[0]: row[1:] for row in fetch(dsn, query)}


def c1_mismatches(expected: dict[int, Row], observed: dict[int, Row]) -> list[Mismatch]:
    """C1 event preservation: each reference event maps to one ledger row with equal fields."""
    mismatches = []
    for key in sorted(expected.keys() | observed.keys()):
        ref, cand = expected.get(key), observed.get(key)
        if ref is None or cand is None:
            present = typed_value(key)
            mismatches.append(
                Mismatch(
                    key=(present,),
                    expected=present if ref is not None else None,
                    observed=present if cand is not None else None,
                )
            )
            continue
        for column, ref_value, cand_value in zip(C1_COLUMNS, ref, cand, strict=True):
            if typed_value(ref_value) != typed_value(cand_value):
                mismatches.append(
                    Mismatch(
                        key=(typed_value(key),),
                        column=column,
                        expected=typed_value(ref_value),
                        observed=typed_value(cand_value),
                    )
                )
    return mismatches


def c1_result(expected: dict[int, Row], observed: dict[int, Row]) -> CheckResult:
    mismatches = c1_mismatches(expected, observed)
    return CheckResult(
        check_id="c1.event_preservation",
        contract_id="commerce.c1_event_preservation",
        contract_version=1,
        required=True,
        outcome=CheckOutcome.FAIL if mismatches else CheckOutcome.PASS,
        expected_key_count=len(expected),
        observed_key_count=len(observed),
        mismatch_count=len(mismatches),
        mismatch_examples=tuple(mismatches[:20]),
        evidence_ids=("ev-c1-event-preservation",),
        dataset_digest=PUBLISHED_DIGEST,
    )


def test_reference_fixture_satisfies_the_rules_in_postgres(reference: str) -> None:
    orders_without_items = scalar(
        reference,
        "SELECT count(*) FROM orders o"
        " WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id)",
    )
    # Reference-first rule for C2: evaluated on the reference before any candidate.
    over_refunded = scalar(
        reference,
        "SELECT count(*) FROM (SELECT order_id,"
        " coalesce(sum(amount) FILTER (WHERE event_kind = 'REFUND'), 0) AS refunded,"
        " coalesce(sum(amount) FILTER (WHERE event_kind = 'CAPTURE'), 0) AS captured"
        " FROM legacy_payment_events GROUP BY order_id) totals WHERE refunded > captured",
    )
    assert (orders_without_items, over_refunded) == (0, 0)
    assert scalar(reference, "SELECT count(*) FROM legacy_payment_events") == len(
        build_fixture().events
    )


def test_clean_control_safe_change_passes_c1(reference: str, new_database: DatabaseFactory) -> None:
    result = c1_result(
        reference_events(reference), candidate_ledger(candidate(new_database, SAFE_CHANGE))
    )
    assert result.outcome is CheckOutcome.PASS
    assert result.mismatch_count == 0
    assert result.expected_key_count == result.observed_key_count == len(build_fixture().events)


def test_detection_join_change_executes_but_c1_fails(
    reference: str, new_database: DatabaseFactory
) -> None:
    candidate_dsn = candidate(new_database, JOIN_CHANGE)  # raises if the migration fails
    expected, observed = reference_events(reference), candidate_ledger(candidate_dsn)

    # Everything a count, key, uniqueness or null-profile check looks at still matches.
    assert scalar(candidate_dsn, "SELECT count(*) FROM ledger_entries") == len(expected)
    assert scalar(
        candidate_dsn, "SELECT count(DISTINCT source_event_id) FROM ledger_entries"
    ) == len(expected)
    assert observed.keys() == expected.keys()
    assert all(
        value is not None for row in [*expected.values(), *observed.values()] for value in row
    )

    result = c1_result(expected, observed)
    assert result.outcome is CheckOutcome.FAIL
    refund_1002 = Mismatch(
        key=(typed_value(1002),),
        column="amount",
        expected=typed_value(Decimal("49.99")),
        observed=typed_value(Decimal("99.98")),
    )
    assert refund_1002 in result.mismatch_examples
    assert CheckResult.model_validate_json(result.model_dump_json()) == result

    # The mechanism, exactly: every event on an order with k > 1 items is multiplied by k, and
    # nothing else differs. Captures are multiplied too, so C3 capture totals also fail.
    fixture = build_fixture()
    items_per_order = Counter(item.order_id for item in fixture.items)
    mismatches = c1_mismatches(expected, observed)
    assert {m.column for m in mismatches} == {"amount"}
    assert len(mismatches) == sum(1 for e in fixture.events if items_per_order[e.order_id] > 1)
    for event_id, (order_id, kind, amount, _) in expected.items():
        assert observed[event_id][2] == amount * items_per_order[order_id], (event_id, kind)

    # Honest baseline comparison: an aggregate SUM over the refund amount also catches this fault.
    refunds = [row[2] for row in expected.values() if row[1] == "REFUND"]
    migrated_refunds = [observed[k][2] for k, row in expected.items() if row[1] == "REFUND"]
    assert sum(migrated_refunds) != sum(refunds)


def test_reapplying_head_is_a_no_op(new_database: DatabaseFactory) -> None:
    candidate_dsn = candidate(new_database, SAFE_CHANGE)
    before = fetch(candidate_dsn, "SELECT * FROM ledger_entries ORDER BY source_event_id")
    migrate(candidate_dsn, SAFE_CHANGE)
    assert fetch(candidate_dsn, "SELECT * FROM ledger_entries ORDER BY source_event_id") == before
    assert scalar(candidate_dsn, "SELECT version_num FROM alembic_version") == "5c1e9a7b3d20"
