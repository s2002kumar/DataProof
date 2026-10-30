from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest

from commerce_scenario import (
    F1_DEMO_ORDER_ID,
    F3_TIE_ORDER_ID,
    PUBLISHED_DIGEST,
    Fixture,
    build_fixture,
    fixture_violations,
)


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    return build_fixture()


def test_fixture_is_deterministic_and_matches_the_published_digest(fixture: Fixture) -> None:
    assert build_fixture().digest() == fixture.digest() == PUBLISHED_DIGEST


def test_another_seed_is_another_fixture(fixture: Fixture) -> None:
    assert build_fixture(seed=1).digest() != fixture.digest()


def test_fixture_satisfies_every_rule(fixture: Fixture) -> None:
    assert fixture_violations(fixture) == []


def test_f1_demonstration_order(fixture: Fixture) -> None:
    items = [i for i in fixture.items if i.order_id == F1_DEMO_ORDER_ID]
    events = [
        (e.event_id, e.event_kind, e.amount)
        for e in fixture.events
        if e.order_id == F1_DEMO_ORDER_ID
    ]
    assert len(items) == 2
    assert events == [(1001, "CAPTURE", Decimal("120.00")), (1002, "REFUND", Decimal("49.99"))]


def test_f3_tie_order(fixture: Fixture) -> None:
    refunds = [
        e for e in fixture.events if e.order_id == F3_TIE_ORDER_ID and e.event_kind == "REFUND"
    ]
    first, second = sorted(refunds, key=lambda e: e.event_id)
    assert first.occurred_at == second.occurred_at
    assert first.amount != second.amount
    assert first.external_ref > second.external_ref


def _without_items_for(fixture: Fixture, order_id: int) -> Fixture:
    return replace(fixture, items=tuple(i for i in fixture.items if i.order_id != order_id))


def _refund_of_everything(fixture: Fixture) -> Fixture:
    events = tuple(
        replace(e, amount=Decimal("999.99")) if e.event_id == 1002 else e for e in fixture.events
    )
    return replace(fixture, events=events)


def _untied(fixture: Fixture) -> Fixture:
    events = tuple(
        replace(e, external_ref="RF-2-C") if e.external_ref == "RF-2-A" else e
        for e in fixture.events
    )
    return replace(fixture, events=events)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda f: _without_items_for(f, 101), "order 101 has no items"),
        (_refund_of_everything, "order 1 refunds 999.99 > captures 120.00"),
        (_untied, "no refund pair tied on occurred_at"),
        (
            lambda f: replace(f, orders=tuple(replace(o, currency="USD") for o in f.orders)),
            "fewer than two currencies",
        ),
    ],
)
def test_rule_violations_are_reported(
    fixture: Fixture, mutate: Callable[[Fixture], Fixture], message: str
) -> None:
    violations = fixture_violations(mutate(fixture))
    assert any(message in v for v in violations), violations
