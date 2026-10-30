"""The sample app's API on real PostgreSQL: the base version and the ledger version.

The base version reads ``legacy_payment_events``. The ledger version, which the ledger change
sets' pull requests carry, reads ``ledger_entries`` and serves refunds as transactions. On the
safe change both agree for every order; on F1 the API itself shows the multiplied refund.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from commerce_env import JOIN_CHANGE, SAFE_CHANGE, baseline, candidate, database_factory, fetch
from sample_app_env import BASE_PACKAGE, api_client, import_app, ledger_package

pytestmark = pytest.mark.integration


@dataclass
class Apis:
    base: TestClient
    ledger_safe: TestClient
    ledger_f1: TestClient
    reference_dsn: str


@pytest.fixture(scope="module")
def clients(postgres_dsn: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Apis]:
    ledger = ledger_package(tmp_path_factory.mktemp("ledger-app"))
    with database_factory(postgres_dsn) as create:
        reference = baseline(create)
        safe = candidate(create, SAFE_CHANGE)
        faulty = candidate(create, JOIN_CHANGE)
        with (
            api_client(BASE_PACKAGE, "commerce_api_base", reference) as base,
            api_client(ledger, "commerce_api_safe", safe) as ledger_safe,
            api_client(ledger, "commerce_api_f1", faulty) as ledger_f1,
        ):
            yield Apis(base, ledger_safe, ledger_f1, reference)


def test_health(clients: Apis) -> None:
    assert clients.base.get("/health").json() == {"status": "ok"}


def test_orders_are_listed_in_pages(clients: Apis) -> None:
    first = clients.base.get("/orders", params={"limit": 3}).json()
    assert [order["order_id"] for order in first] == [1, 2, 3]
    after = clients.base.get("/orders", params={"limit": 2, "after": 3}).json()
    assert [order["order_id"] for order in after] == [4, 5]
    assert clients.base.get("/orders", params={"limit": 0}).status_code == 422


def test_order_detail_has_exact_money_and_instants(clients: Apis) -> None:
    order = clients.base.get("/orders/1").json()
    assert order["captured_total"] == "120.00"
    assert order["refunded_total"] == "49.99"
    assert len(order["items"]) == 2
    assert order["placed_at"].endswith(("Z", "+00:00"))
    assert all(isinstance(item["unit_price"], str) for item in order["items"])


def test_base_refunds_route(clients: Apis) -> None:
    refunds = clients.base.get("/orders/1/refunds").json()
    assert [(r["event_id"], r["amount"]) for r in refunds] == [(1002, "49.99")]
    tie = clients.base.get("/orders/2/refunds").json()
    assert [r["event_id"] for r in tie] == [1004, 1005], "same instant: ordered by event ID"


@pytest.mark.parametrize("path", ["/orders/999999", "/orders/999999/refunds"])
def test_missing_orders_are_not_found(clients: Apis, path: str) -> None:
    assert clients.base.get(path).status_code == 404


def test_ledger_version_replaces_refunds_with_transactions(
    clients: Apis,
) -> None:
    ledger = clients.ledger_safe
    assert ledger.get("/orders/1/refunds").status_code == 404
    refunds = ledger.get("/orders/1/transactions", params={"kind": "REFUND"}).json()
    assert [(t["source_event_id"], t["kind"], t["amount"]) for t in refunds] == [
        (1002, "REFUND", "49.99")
    ]
    everything = ledger.get("/orders/1/transactions").json()
    assert {t["kind"] for t in everything} == {"CAPTURE", "REFUND"}
    assert ledger.get("/orders/1/transactions", params={"kind": "OTHER"}).status_code == 422
    assert ledger.get("/orders/999999/transactions").status_code == 404


def test_safe_change_keeps_every_order_total_through_the_api(
    clients: Apis,
) -> None:
    reference_dsn = clients.reference_dsn
    order_ids = [row[0] for row in fetch(reference_dsn, "SELECT order_id FROM orders ORDER BY 1")]
    for order_id in order_ids:
        before = clients.base.get(f"/orders/{order_id}").json()
        after = clients.ledger_safe.get(f"/orders/{order_id}").json()
        assert after == before, order_id


def test_f1_multiplied_refund_is_visible_through_the_api(clients: Apis) -> None:
    before = clients.base.get("/orders/1").json()
    after = clients.ledger_f1.get("/orders/1").json()
    assert (before["refunded_total"], after["refunded_total"]) == ("49.99", "99.98")
    assert after["captured_total"] != before["captured_total"], "F1 also inflates captures"


def test_the_app_needs_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _, db = import_app(BASE_PACKAGE, "commerce_api_settings")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        db._conninfo()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u@h:5432/app")
    assert db._conninfo() == "postgresql://u@h:5432/app"
