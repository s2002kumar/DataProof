import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "packages" / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"
SCHEMAS_DIR = CONTRACTS_DIR / "schemas"

ExampleLoader = Callable[[str], dict[str, Any]]


@pytest.fixture
def example() -> ExampleLoader:
    """Load a committed example document (a known-valid baseline) as a mutable dict."""

    def load(name: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((EXAMPLES_DIR / f"{name}.json").read_text("utf-8"))
        return data

    return load


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A real PostgreSQL: ``DATAPROOF_TEST_DATABASE_URL`` (CI service) or a testcontainer.

    There is deliberately no skip path. Without a database the integration tests error, so a
    missing PostgreSQL can never pass as green.
    """
    configured = os.environ.get("DATAPROOF_TEST_DATABASE_URL")
    if configured:
        yield configured
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine", driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture
def pg(postgres_dsn: str) -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    with psycopg.connect(postgres_dsn) as connection:
        yield connection
        connection.rollback()
