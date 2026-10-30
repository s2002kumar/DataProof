"""Typed comparators and keyed row comparison (docs/VERIFICATION.md, row matching)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import TypeGuard

from dataproof_contracts import Comparator, ComparatorKind, Mismatch, TypedValue, typed_value

Key = tuple[object, ...]
Row = tuple[object, ...]


def _exact_number(value: object) -> TypeGuard[Decimal | int]:
    return isinstance(value, Decimal | int) and not isinstance(value, bool)


def values_match(comparator: Comparator, expected: object, observed: object) -> bool:
    """Whether two values are equal under a comparator. NULL equals only NULL.

    ``DECIMAL`` compares exact numbers within the declared tolerance. Everything else is typed
    equality, which compares timestamps as instants (``TimestampValue`` pins a fixed offset), so
    ``EXACT`` and ``INSTANT`` agree on timestamps; ``INSTANT`` documents the intent. Raises
    ``TypeError`` for floats, which cannot be compared exactly.
    """
    if (
        comparator.kind is ComparatorKind.DECIMAL
        and _exact_number(expected)
        and _exact_number(observed)
    ):
        return abs(Decimal(expected) - Decimal(observed)) <= comparator.tolerance
    return typed_value(expected) == typed_value(observed)


def _sort_key(key: Key) -> tuple[tuple[int, object], ...]:
    """Deterministic key order: by type rank, then by natural order within the type."""
    parts: list[tuple[int, object]] = []
    for part in key:
        if part is None:
            parts.append((0, 0))
        elif isinstance(part, bool):
            parts.append((1, part))
        elif _exact_number(part):
            parts.append((2, Decimal(part)))
        elif isinstance(part, datetime):
            parts.append((3, part.timestamp()))
        else:
            parts.append((4, str(part)))
    return tuple(parts)


def compare_keyed(
    expected: Mapping[Key, Row],
    observed: Mapping[Key, Row],
    columns: Sequence[str],
    comparators: Mapping[str, Comparator],
) -> list[Mismatch]:
    """All differences between keyed reference and candidate rows, in key order.

    A key missing on either side is a presence mismatch: a binding cannot narrow the oracle's
    key universe, and the candidate cannot add rows the oracle does not expect.
    """
    mismatches: list[Mismatch] = []
    for key in sorted(expected.keys() | observed.keys(), key=_sort_key):
        reference, candidate = expected.get(key), observed.get(key)
        typed_key: tuple[TypedValue, ...] = tuple(typed_value(part) for part in key)
        if reference is None or candidate is None:
            marker = typed_key[0]
            mismatches.append(
                Mismatch(
                    key=typed_key,
                    expected=marker if reference is not None else None,
                    observed=marker if candidate is not None else None,
                )
            )
            continue
        for column, ref_value, cand_value in zip(columns, reference, candidate, strict=True):
            if not values_match(comparators[column], ref_value, cand_value):
                mismatches.append(
                    Mismatch(
                        key=typed_key,
                        column=column,
                        expected=typed_value(ref_value),
                        observed=typed_value(cand_value),
                    )
                )
    return mismatches
