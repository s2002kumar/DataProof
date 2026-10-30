"""Deterministic Commerce Orders & Refunds baseline fixture.

Implements the fixture rules in docs/SCENARIO_CONTRACT.md. Rows come from a fixed seed through
plain Python (never from a model), every rule is checked by ``fixture_violations``, and the
fixture is identified by a SHA-256 digest of its canonical form. All identities are fictional.

Changing what the generator produces is a new fixture version: bump ``FIXTURE_VERSION`` and
republish ``PUBLISHED_DIGEST``.
"""

import hashlib
import json
import random
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import astuple, dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal

FIXTURE_VERSION = 1
SEED = 20261030
RANDOM_ORDER_COUNT = 200
CUSTOMER_COUNT = 40
PUBLISHED_DIGEST = "b4f47bea876de4049bedf0968fda8c055f327938fc5010c990d75bfe9e1a0530"

F1_DEMO_ORDER_ID = 1
"""Exactly two items and a 49.99 refund (event 1002)."""
F3_TIE_ORDER_ID = 2
"""Two refunds at one instant whose external_ref order is the reverse of their event_id order."""

DST_TRANSITIONS = (
    datetime(2026, 3, 8, 7, 0, tzinfo=UTC),  # America/Toronto springs forward
    datetime(2026, 3, 29, 1, 0, tzinfo=UTC),  # Europe/Paris springs forward
    datetime(2026, 11, 1, 6, 0, tzinfo=UTC),  # America/Toronto falls back
)

CENT = Decimal("0.01")
PRICE_STEP = Decimal("0.0001")
EventKind = Literal["CAPTURE", "REFUND"]


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: int
    display_name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Order:
    order_id: int
    customer_id: int
    currency: str
    status: str
    placed_at: datetime
    note: str | None


@dataclass(frozen=True, slots=True)
class OrderItem:
    order_id: int
    line_no: int
    sku: str
    unit_price: Decimal
    quantity: int


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    event_id: int
    order_id: int
    event_kind: EventKind
    amount: Decimal
    occurred_at: datetime
    external_ref: str
    memo: str | None


Row = tuple[object, ...]


def _canonical(value: object) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        return json.dumps(value)
    return str(value)  # int, Decimal (scale preserved)


@dataclass(frozen=True)
class Fixture:
    seed: int
    customers: tuple[Customer, ...]
    orders: tuple[Order, ...]
    items: tuple[OrderItem, ...]
    events: tuple[PaymentEvent, ...]

    def tables(self) -> Iterator[tuple[str, tuple[str, ...], list[Row]]]:
        """Tables in foreign-key order, each with its column names and rows sorted by key."""
        layout: Sequence[
            tuple[str, type, Sequence[Customer | Order | OrderItem | PaymentEvent]]
        ] = (
            ("customers", Customer, self.customers),
            ("orders", Order, self.orders),
            ("order_items", OrderItem, self.items),
            ("legacy_payment_events", PaymentEvent, self.events),
        )
        for table, row_type, records in layout:
            columns = tuple(f.name for f in fields(row_type))
            rows = sorted((astuple(r) for r in records), key=lambda r: (r[0], r[1]))
            yield table, columns, rows

    def digest(self) -> str:
        sha = hashlib.sha256(f"dataproof-commerce-fixture/{FIXTURE_VERSION}\n".encode())
        for table, _, rows in self.tables():
            for row in rows:
                sha.update(("\t".join([table, *map(_canonical, row)]) + "\n").encode())
        return sha.hexdigest()


class _Builder:
    def __init__(self) -> None:
        self.orders: list[Order] = []
        self.items: list[OrderItem] = []
        self.events: list[PaymentEvent] = []
        self._next_event_id = 1001

    def add_order(
        self,
        order_id: int,
        customer_id: int,
        currency: str,
        placed_at: datetime,
        items: Sequence[tuple[str, Decimal, int]],
        events: Sequence[tuple[EventKind, Decimal, datetime, str | None]],
        note: str | None = None,
        refs: Sequence[str] | None = None,
    ) -> None:
        for line_no, (sku, price, quantity) in enumerate(items, start=1):
            self.items.append(OrderItem(order_id, line_no, sku, price, quantity))
        counters: dict[str, int] = defaultdict(int)
        for index, (kind, amount, at, memo) in enumerate(events):
            counters[kind] += 1
            prefix = "CAP" if kind == "CAPTURE" else "RF"
            ref = refs[index] if refs else f"{prefix}-{order_id}-{counters[kind]}"
            self.events.append(
                PaymentEvent(self._next_event_id, order_id, kind, amount, at, ref, memo)
            )
            self._next_event_id += 1
        captured = sum((a for k, a, _, _ in events if k == "CAPTURE"), Decimal(0))
        refunded = sum((a for k, a, _, _ in events if k == "REFUND"), Decimal(0))
        status = (
            "PAID" if not refunded else "REFUNDED" if refunded == captured else "PARTIALLY_REFUNDED"
        )
        self.orders.append(Order(order_id, customer_id, currency, status, placed_at, note))


def _at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _dec(text: str) -> Decimal:
    return Decimal(text)


def _special_orders(b: _Builder) -> None:
    """Hand-built rows that guarantee the edge cases the fixture rules require."""
    b.add_order(  # F1 demonstration order
        F1_DEMO_ORDER_ID,
        1,
        "USD",
        _at(2026, 2, 10, 14, 55),
        [("SKU-TEE", _dec("30.0000"), 2), ("SKU-MUG", _dec("60.0000"), 1)],
        [
            ("CAPTURE", _dec("120.00"), _at(2026, 2, 10, 15, 0), None),
            ("REFUND", _dec("49.99"), _at(2026, 2, 12, 9, 30), "damaged mug"),
        ],
    )
    b.add_order(  # F3 tie: same order, kind and instant; external_ref order reversed
        F3_TIE_ORDER_ID,
        2,
        "USD",
        _at(2026, 4, 1, 11, 0),
        [
            ("SKU-LAMP", _dec("50.0000"), 1),
            ("SKU-SHADE", _dec("75.0000"), 2),
            ("SKU-GIFT", _dec("0.0000"), 1),
        ],
        [
            ("CAPTURE", _dec("200.00"), _at(2026, 4, 1, 11, 5), None),
            ("REFUND", _dec("12.50"), _at(2026, 4, 2, 9, 0), None),
            ("REFUND", _dec("37.49"), _at(2026, 4, 2, 9, 0), None),
        ],
        refs=["CAP-2-1", "RF-2-B", "RF-2-A"],
    )
    b.add_order(  # Toronto spring-forward; zero refunds
        3,
        3,
        "USD",
        _at(2026, 3, 8, 6, 30),
        [("SKU-BOOK", _dec("18.9900"), 1)],
        [("CAPTURE", _dec("18.99"), _at(2026, 3, 8, 7, 30), None)],
    )
    b.add_order(  # Toronto fall-back: both events at 01:45 local wall-clock time
        4,
        4,
        "USD",
        _at(2026, 11, 1, 5, 30),
        [("SKU-SCARF", _dec("42.5000"), 1)],
        [
            ("CAPTURE", _dec("42.50"), _at(2026, 11, 1, 5, 45), None),
            ("REFUND", _dec("42.50"), _at(2026, 11, 1, 6, 45), None),
        ],
    )
    b.add_order(  # Paris spring-forward, second currency
        5,
        5,
        "EUR",
        _at(2026, 3, 29, 0, 30),
        [("SKU-KETTLE", _dec("64.9000"), 1)],
        [("CAPTURE", _dec("64.90"), _at(2026, 3, 29, 1, 30), None)],
    )
    b.add_order(  # large-valued fractional amounts, split capture
        6,
        6,
        "USD",
        _at(2026, 5, 5, 10, 0),
        [("SKU-SERVER-RACK", _dec("9876543210.9900"), 1)],
        [
            ("CAPTURE", _dec("9876543210.00"), _at(2026, 5, 5, 10, 5), None),
            ("CAPTURE", _dec("0.99"), _at(2026, 5, 5, 10, 6), None),
            ("REFUND", _dec("0.99"), _at(2026, 5, 7, 16, 0), None),
        ],
    )
    b.add_order(  # same-cent amounts on one order
        7,
        7,
        "EUR",
        _at(2026, 6, 6, 12, 0),
        [("SKU-PEN", _dec("10.0100"), 2)],
        [
            ("CAPTURE", _dec("10.01"), _at(2026, 6, 6, 12, 1), None),
            ("CAPTURE", _dec("10.01"), _at(2026, 6, 6, 12, 2), None),
            ("REFUND", _dec("10.01"), _at(2026, 6, 8, 12, 0), None),
        ],
    )
    b.add_order(  # empty strings next to NULLs
        8,
        8,
        "USD",
        _at(2026, 7, 7, 8, 0),
        [("SKU-CAP", _dec("15.0000"), 1)],
        [
            ("CAPTURE", _dec("15.00"), _at(2026, 7, 7, 8, 1), ""),
            ("REFUND", _dec("5.00"), _at(2026, 7, 9, 8, 0), None),
        ],
        note="",
    )


def _random_orders(b: _Builder, rng: random.Random, count: int) -> None:
    start = _at(2026, 1, 5, 0, 0)
    for order_id in range(101, 101 + count):
        customer_id = rng.randint(1, CUSTOMER_COUNT)
        currency = rng.choice(("USD", "EUR"))
        placed_at = start + timedelta(minutes=rng.randint(0, 260 * 24 * 60))
        items = [
            (
                f"SKU-{rng.randint(1, 60):03d}",
                Decimal(rng.randint(100, 2_500_000)) * PRICE_STEP,
                rng.randint(1, 5),
            )
            for _ in range(rng.randint(1, 4))
        ]
        total = sum((price * qty for _, price, qty in items), Decimal(0)).quantize(CENT)
        captures = [total]
        if total >= 2 * CENT and rng.random() < 0.3:
            first = (total * rng.randint(20, 80) / 100).quantize(CENT)
            first = min(max(first, CENT), total - CENT)
            captures = [first, total - first]
        at = placed_at
        events: list[tuple[EventKind, Decimal, datetime, str | None]] = []
        for amount in captures:
            at += timedelta(minutes=rng.randint(1, 120))
            events.append(("CAPTURE", amount, at, None))
        remaining = total
        for _ in range(rng.choices((0, 1, 2), weights=(50, 35, 15))[0]):
            amount = min(
                max((remaining * rng.randint(5, 60) / 100).quantize(CENT), CENT), remaining
            )
            if amount < CENT or remaining < CENT:
                break
            remaining -= amount
            at += timedelta(hours=rng.randint(1, 240))
            events.append(
                ("REFUND", amount, at, rng.choice((None, None, None, "", "customer request")))
            )
        b.add_order(
            order_id,
            customer_id,
            currency,
            placed_at,
            items,
            events,
            note=rng.choice((None, None, None, "gift wrap")),
        )


def build_fixture(seed: int = SEED, random_orders: int = RANDOM_ORDER_COUNT) -> Fixture:
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not security
    builder = _Builder()
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        _special_orders(builder)
        _random_orders(builder, rng, random_orders)
    customers = tuple(
        Customer(n, f"Fictional Customer {n:03d}", _at(2025, 6, 1, 9, 0) + timedelta(days=n))
        for n in range(1, CUSTOMER_COUNT + 1)
    )
    return Fixture(
        seed, customers, tuple(builder.orders), tuple(builder.items), tuple(builder.events)
    )


def fixture_violations(fixture: Fixture) -> list[str]:
    """Every fixture rule that does not hold; empty when the fixture is valid."""
    problems: list[str] = []
    customer_ids = {c.customer_id for c in fixture.customers}
    orders = {o.order_id: o for o in fixture.orders}
    items_by_order: dict[int, list[OrderItem]] = defaultdict(list)
    for item in fixture.items:
        items_by_order[item.order_id].append(item)
    events_by_order: dict[int, list[PaymentEvent]] = defaultdict(list)
    for event in fixture.events:
        events_by_order[event.order_id].append(event)

    problems += [
        f"order {o} has an unknown customer"
        for o, r in orders.items()
        if r.customer_id not in customer_ids
    ]
    problems += [f"order {o} has no items" for o in orders if not items_by_order[o]]
    problems += [
        f"event {e.event_id} has an unknown order"
        for e in fixture.events
        if e.order_id not in orders
    ]
    for order_id, events in events_by_order.items():
        captured = sum((e.amount for e in events if e.event_kind == "CAPTURE"), Decimal(0))
        refunded = sum((e.amount for e in events if e.event_kind == "REFUND"), Decimal(0))
        if refunded > captured:
            problems.append(f"order {order_id} refunds {refunded} > captures {captured}")
    for event in fixture.events:
        if event.amount <= 0 or event.amount != event.amount.quantize(CENT):
            problems.append(f"event {event.event_id} amount {event.amount} is not positive cents")
    refs = [e.external_ref for e in fixture.events]
    if len(set(refs)) != len(refs):
        problems.append("external_ref values are not unique")

    if len({o.currency for o in fixture.orders}) < 2:
        problems.append("fewer than two currencies")
    if all(any(e.event_kind == "REFUND" for e in events_by_order[o]) for o in orders):
        problems.append("no zero-refund order")
    if not any(len(items_by_order[o]) > 1 for o in orders):
        problems.append("no order with multiple items")
    memos = {e.memo for e in fixture.events}
    if None not in memos or "" not in memos:
        problems.append("fixture needs both NULL and empty-string memos")
    if not any(e.amount >= 1_000_000_000 for e in fixture.events):
        problems.append("no large-valued amount")
    for transition in DST_TRANSITIONS:
        if not any(abs(e.occurred_at - transition) <= timedelta(hours=1) for e in fixture.events):
            problems.append(f"no event within an hour of the DST transition at {transition}")

    demo_refunds = [e.amount for e in events_by_order[F1_DEMO_ORDER_ID] if e.event_kind == "REFUND"]
    if len(items_by_order[F1_DEMO_ORDER_ID]) != 2 or Decimal("49.99") not in demo_refunds:
        problems.append("F1 demonstration order must have exactly two items and a 49.99 refund")
    if not any(_has_reversed_tie(events_by_order[o]) for o in orders):
        problems.append("no refund pair tied on occurred_at with reversed external_ref order")
    return problems


def _has_reversed_tie(events: Sequence[PaymentEvent]) -> bool:
    refunds = sorted((e for e in events if e.event_kind == "REFUND"), key=lambda e: e.event_id)
    return any(
        a.occurred_at == b.occurred_at and a.amount != b.amount and a.external_ref > b.external_ref
        for i, a in enumerate(refunds)
        for b in refunds[i + 1 :]
    )
