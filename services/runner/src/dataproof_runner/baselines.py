"""Versioned baselines loaded identically into the reference and candidate databases."""

import hashlib
from typing import Any, Protocol

import psycopg
from psycopg import sql

from commerce_scenario import build_fixture, load_fixture


class Baseline(Protocol):
    name: str
    tables: tuple[str, ...]

    def digest(self) -> str:
        """The dataset digest that ``RunInputs.dataset_digest`` must equal."""
        ...

    def load(self, connection: psycopg.Connection[Any]) -> None: ...


class CommerceBaseline:
    """The published Commerce Orders & Refunds fixture (``scenarios/commerce``)."""

    name = "commerce-v1"
    tables: tuple[str, ...] = ("customers", "orders", "order_items", "legacy_payment_events")

    def __init__(self) -> None:
        self._fixture = build_fixture()

    def digest(self) -> str:
        return self._fixture.digest()

    def load(self, connection: psycopg.Connection[Any]) -> None:
        load_fixture(connection, self._fixture)


def baseline_for(name: str) -> Baseline:
    if name == CommerceBaseline.name:
        return CommerceBaseline()
    raise ValueError(f"unknown baseline: {name!r}")


def user_tables(connection: psycopg.Connection[Any]) -> tuple[str, ...]:
    """Every table in the application schema, sorted."""
    rows = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def database_digest(connection: psycopg.Connection[Any], tables: tuple[str, ...]) -> str:
    """SHA-256 over every row of the given tables, as PostgreSQL renders them.

    Computed in both environments after loading, it shows that the reference and the candidate
    start from identical data, not just from the same fixture file.
    """
    digest = hashlib.sha256()
    with connection.transaction():
        connection.execute("SET LOCAL TIME ZONE 'UTC'")
        connection.execute("SET LOCAL DateStyle = 'ISO, YMD'")
        connection.execute("SET LOCAL extra_float_digits = 3")
        for table in tables:
            query = sql.SQL(
                "SELECT count(*), encode(sha256(convert_to("
                "coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), ''), 'UTF8')), 'hex') "
                "FROM {} AS t"
            ).format(sql.Identifier(table))
            row = connection.execute(query).fetchone()
            if row is None:
                raise RuntimeError(f"no digest for {table}")
            digest.update(f"{table}\t{row[0]}\t{row[1]}\n".encode())
    return digest.hexdigest()
