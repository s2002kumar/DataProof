"""FK-closure subsets of the real commerce baseline (no Docker)."""

from collections.abc import Iterator
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import sql

from commerce_env import DatabaseFactory, baseline, database_factory, fetch, migrate
from dataproof_runner.subset import Catalog, closure, digest, export, load

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def reference(postgres_dsn: str) -> Iterator[str]:
    with database_factory(postgres_dsn) as create:
        yield baseline(create)


def _catalog(dsn: str) -> Catalog:
    with psycopg.connect(dsn) as conn:
        return Catalog.read(conn)


def test_the_catalog_finds_the_commerce_relationships(reference: str) -> None:
    catalog = _catalog(reference)
    edges = {(fk.child, fk.parent) for fk in catalog.foreign_keys}
    assert edges == {
        ("orders", "customers"),
        ("order_items", "orders"),
        ("legacy_payment_events", "orders"),
    }
    assert catalog.primary_keys["order_items"] == ("order_id", "line_no")
    assert catalog.load_order(["legacy_payment_events", "order_items", "orders", "customers"]) == [
        "customers",
        "orders",
        "legacy_payment_events",
        "order_items",
    ]


@pytest.mark.parametrize(
    ("keys", "table"),
    [
        (["event_id"], "legacy_payment_events"),
        (["order_id", "currency"], "orders"),
        (["order_id"], "orders"),
        (["event_kind"], None),
    ],
)
def test_the_seed_table_holds_the_oracle_keys(
    reference: str, keys: list[str], table: str | None
) -> None:
    assert _catalog(reference).seed_table(keys) == table


def _rows(dsn: str, query: LiteralString, *params: Any) -> set[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return {tuple(row) for row in conn.execute(query, params).fetchall()}


def test_a_refund_brings_its_aggregate_and_everything_it_references(reference: str) -> None:
    catalog = _catalog(reference)
    with psycopg.connect(reference) as conn:
        subset = closure(conn, catalog, "legacy_payment_events", ["event_id"], [(1002,)])
    ((order_id,),) = _rows(
        reference, "SELECT order_id FROM legacy_payment_events WHERE event_id = 1002"
    )
    ((customer_id,),) = _rows(
        reference, "SELECT customer_id FROM orders WHERE order_id = %s", order_id
    )
    assert subset["orders"] == {(order_id,)}
    assert subset["customers"] == {(customer_id,)}
    assert subset["legacy_payment_events"] == _rows(
        reference, "SELECT event_id FROM legacy_payment_events WHERE order_id = %s", order_id
    ), "the refund's sibling events come along"
    assert subset["order_items"] == _rows(
        reference, "SELECT order_id, line_no FROM order_items WHERE order_id = %s", order_id
    ), "so does every item of its order"


def test_an_exported_subset_loads_into_an_empty_schema_with_integrity(
    reference: str, new_database: DatabaseFactory
) -> None:
    catalog = _catalog(reference)
    with psycopg.connect(reference) as conn:
        subset = closure(conn, catalog, "orders", ["order_id"], [(1,), (2,)])
        data = export(conn, catalog, subset)
        again = export(conn, catalog, subset)
    assert digest(data) == digest(again)
    assert list(data) == catalog.load_order(data)

    target = new_database()
    migrate(target, change=None)
    with psycopg.connect(target) as conn:
        load(conn, data)
    with psycopg.connect(target) as conn:
        counts = {
            table: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
            for table in data
        }
    assert counts == {table: (len(rows),) for table, rows in subset.items()}
    assert fetch(
        target, "SELECT amount::text FROM legacy_payment_events WHERE event_id = 1002"
    ) == [("49.99",)]
