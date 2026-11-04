"""C8: a candidate column's declared type can hold what the contract compares."""

from dataproof_contracts import Comparator, ComparatorKind, Mismatch, TextValue, TypedValue
from dataproof_verifier import ColumnType, type_problems

DECIMAL2 = Comparator(kind=ComparatorKind.DECIMAL, decimal_scale=2)
DECIMAL0 = Comparator(kind=ComparatorKind.DECIMAL, decimal_scale=0)
INSTANT = Comparator(kind=ComparatorKind.INSTANT)
EXACT = Comparator(kind=ComparatorKind.EXACT)


def column(
    name: str, oid: int, display: str, precision: int | None = None, scale: int | None = None
) -> ColumnType:
    return ColumnType(name=name, oid=oid, type_display=display, precision=precision, scale=scale)


def _text(value: TypedValue | None) -> str:
    assert isinstance(value, TextValue)
    return value.value


def _summary(problems: list[Mismatch]) -> dict[str, tuple[str, str]]:
    return {_text(p.key[0]): (_text(p.expected), _text(p.observed)) for p in problems}


def test_money_needs_a_numeric_with_the_contracts_scale() -> None:
    types = (
        column("amount", 1700, "numeric", 12, 2),
        column("rounded", 1700, "numeric", 12, 0),
        column("cents", 23, "integer"),
        column("approx", 701, "double precision"),
        column("computed", 1700, "numeric"),
    )
    expected = {
        "amount": DECIMAL2,
        "rounded": DECIMAL2,
        "cents": DECIMAL2,
        "approx": DECIMAL2,
        "computed": DECIMAL2,
    }
    assert _summary(type_problems(types, {}, expected)) == {
        "rounded": ("numeric with scale >= 2", "numeric(12, 0)"),
        "cents": ("numeric with scale >= 2", "integer"),
        "approx": ("numeric with scale >= 2", "double precision"),
    }, "a declared scale of 2, an undeclared scale (an expression) and nothing else pass"


def test_whole_units_accept_integers() -> None:
    types = (column("seats", 23, "integer"), column("units", 1700, "numeric", 10, 0))
    assert type_problems(types, {}, {"seats": DECIMAL0, "units": DECIMAL0}) == []


def test_an_instant_needs_a_time_zone() -> None:
    types = (
        column("at", 1184, "timestamp with time zone"),
        column("naive", 1114, "timestamp without time zone"),
        column("day", 1082, "date"),
    )
    problems = type_problems(types, {}, {"at": INSTANT, "naive": INSTANT, "day": INSTANT})
    assert _summary(problems) == {
        "naive": ("timestamp with time zone", "timestamp without time zone"),
        "day": ("timestamp with time zone", "date"),
    }


def test_exact_columns_refuse_only_floats_and_the_mapping_is_followed() -> None:
    types = (column("kind_text", 25, "text"), column("ratio", 700, "real"))
    names = {"kind": "kind_text", "share": "ratio"}
    problems = type_problems(types, names, {"kind": EXACT, "share": EXACT})
    assert list(_summary(problems)) == ["share"]


def test_a_column_the_result_lacks_is_not_a_type_problem() -> None:
    """A missing column is a coverage gap, reported by the data check; not double-counted."""
    assert type_problems((), {}, {"amount": DECIMAL2}) == []
