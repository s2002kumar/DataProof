"""Typed scalar values as they appear in check evidence.

JSON keeps the database type explicit (docs/VERIFICATION.md, "Row matching and
canonicalization"): a NUMERIC is a decimal *string* and is never parsed from or written as a
float, a TIMESTAMPTZ is an ISO-8601 instant with its offset, and SQL NULL is a value of its own,
distinct from an empty string or zero.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
)

from dataproof_contracts._base import Contract


def _reject_inexact(value: object) -> object:
    if isinstance(value, float | bool):
        raise ValueError("decimal values must be strings, integers or Decimal, never floats")
    return value


DECIMAL_STRING_PATTERN = r"^-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"
"""JSON form of a decimal: Python's ``str(Decimal)``, which may use an exponent (``0E-8``)."""

ExactDecimal = Annotated[
    Decimal,
    BeforeValidator(_reject_inexact),
    Field(allow_inf_nan=False, json_schema_extra={"pattern": DECIMAL_STRING_PATTERN}),
]


def _fixed_offset(value: datetime) -> datetime:
    """Pin a datetime to its own UTC offset, dropping any named zone.

    Python compares and subtracts datetimes that share a ``tzinfo`` object on the wall clock and
    ignores ``fold``: with ``ZoneInfo("America/Toronto")``, 01:30 EDT and 01:30 EST on the
    fall-back night compare equal although they are an hour apart. With a fixed offset the
    wall clock *is* the instant, so equality and arithmetic are exact. The offset is kept.
    """
    offset = value.utcoffset()
    if offset is None:
        raise ValueError("naive datetimes are ambiguous; use timezone-aware values")
    return value.replace(tzinfo=timezone(offset), fold=0)


Instant = Annotated[AwareDatetime, AfterValidator(_fixed_offset)]


class NullValue(Contract):
    type: Literal["null"] = "null"


class TextValue(Contract):
    type: Literal["text"] = "text"
    value: StrictStr


class IntegerValue(Contract):
    type: Literal["integer"] = "integer"
    value: StrictInt


class DecimalValue(Contract):
    type: Literal["decimal"] = "decimal"
    value: ExactDecimal


class TimestampValue(Contract):
    type: Literal["timestamptz"] = "timestamptz"
    value: Instant


class BooleanValue(Contract):
    type: Literal["boolean"] = "boolean"
    value: StrictBool


TypedValue = Annotated[
    NullValue | TextValue | IntegerValue | DecimalValue | TimestampValue | BooleanValue,
    Field(discriminator="type"),
]


def typed_value(value: object) -> TypedValue:
    """Wrap a value read from PostgreSQL (for example via psycopg) in its typed form.

    Raises ``TypeError`` for floats, which cannot represent NUMERIC values exactly, and
    ``ValueError`` for naive datetimes, whose instant is ambiguous.
    """
    if value is None:
        return NullValue()
    if isinstance(value, bool):
        return BooleanValue(value=value)
    if isinstance(value, int):
        return IntegerValue(value=value)
    if isinstance(value, Decimal):
        return DecimalValue(value=value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("naive datetimes are ambiguous; use timezone-aware values")
        return TimestampValue(value=value)
    if isinstance(value, str):
        return TextValue(value=value)
    if isinstance(value, float):
        raise TypeError("floats are not exact; read NUMERIC columns as Decimal")
    raise TypeError(f"unsupported value type: {type(value).__name__}")
