from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import TypeAdapter, ValidationError

from dataproof_contracts import (
    BooleanValue,
    DecimalValue,
    IntegerValue,
    Mismatch,
    NullValue,
    TextValue,
    TimestampValue,
    TypedValue,
    typed_value,
)

ADAPTER: TypeAdapter[TypedValue] = TypeAdapter(TypedValue)
EST = timezone(timedelta(hours=-5))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, NullValue()),
        (True, BooleanValue(value=True)),
        (7, IntegerValue(value=7)),
        (Decimal("49.99"), DecimalValue(value=Decimal("49.99"))),
        ("", TextValue(value="")),
        (
            datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
            TimestampValue(value=datetime(2026, 3, 8, 6, 30, tzinfo=UTC)),
        ),
    ],
)
def test_typed_value_keeps_the_database_type(raw: object, expected: TypedValue) -> None:
    assert typed_value(raw) == expected


def test_typed_value_refuses_floats() -> None:
    with pytest.raises(TypeError, match="not exact"):
        typed_value(49.99)


def test_typed_value_refuses_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="naive"):
        typed_value(datetime(2026, 3, 8, 6, 30))  # noqa: DTZ001 - the point of the test


@pytest.mark.parametrize(
    "payload", ['{"type":"decimal","value":49.99}', '{"type":"decimal","value":true}']
)
def test_decimal_json_rejects_inexact_inputs(payload: str) -> None:
    with pytest.raises(ValidationError, match="never floats"):
        ADAPTER.validate_json(payload)


@pytest.mark.parametrize(
    "payload", ['{"type":"decimal","value":"NaN"}', '{"type":"decimal","value":"Infinity"}']
)
def test_decimal_json_rejects_non_finite_values(payload: str) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_json(payload)


def test_decimal_round_trip_preserves_value_and_scale() -> None:
    value = typed_value(Decimal("49.990"))
    payload = ADAPTER.dump_json(value)
    assert payload == b'{"type":"decimal","value":"49.990"}'
    restored = ADAPTER.validate_json(payload)
    assert isinstance(restored, DecimalValue)
    assert restored.value.as_tuple() == Decimal("49.990").as_tuple()


def test_integer_json_rejects_booleans_and_floats() -> None:
    for payload in ('{"type":"integer","value":true}', '{"type":"integer","value":1.0}'):
        with pytest.raises(ValidationError):
            ADAPTER.validate_json(payload)


def test_null_and_empty_string_are_distinct() -> None:
    null, empty = typed_value(None), typed_value("")
    assert null != empty
    assert ADAPTER.dump_json(null) == b'{"type":"null"}'
    assert ADAPTER.dump_json(empty) == b'{"type":"text","value":""}'


def test_ambiguous_fall_back_times_are_different_instants() -> None:
    toronto = ZoneInfo("America/Toronto")
    edt = datetime(2026, 11, 1, 1, 30, tzinfo=toronto)  # fold=0: 05:30 UTC
    est = edt.replace(fold=1)  # same wall clock an hour later: 06:30 UTC
    assert edt == est  # Python ignores fold when both sides share one tzinfo object

    first, second = TimestampValue(value=edt), TimestampValue(value=est)
    assert first != second
    assert second.value - first.value == timedelta(hours=1)
    mismatch = Mismatch(
        key=(typed_value(1),), column="occurred_at", expected=first, observed=second
    )
    assert mismatch.expected != mismatch.observed


def test_timestamps_compare_as_instants_and_keep_their_offset() -> None:
    utc = typed_value(datetime(2026, 3, 8, 6, 30, tzinfo=UTC))
    toronto = typed_value(datetime(2026, 3, 8, 1, 30, tzinfo=EST))
    assert utc == toronto
    assert (
        ADAPTER.dump_json(toronto) == b'{"type":"timestamptz","value":"2026-03-08T01:30:00-05:00"}'
    )
