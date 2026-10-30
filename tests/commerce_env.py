"""Reference and candidate commerce databases for integration tests.

Each environment is its own database, migrated with the sample app's Alembic revisions plus at
most one candidate change set (as the pull request head would add it), and loaded with the
published baseline fixture, as in a real run.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine

from commerce_scenario import build_fixture, load_fixture

REPO = Path(__file__).resolve().parents[1]
SAMPLE_APP = REPO / "sample-app"
SCENARIO = REPO / "scenarios" / "commerce"
CHANGES = SCENARIO / "changes"
ORACLES = SCENARIO / "oracles"
SAFE_CHANGE, JOIN_CHANGE = "ledger-backfill-01", "ledger-backfill-07"

Row = tuple[Any, ...]
DatabaseFactory = Callable[[], str]


@contextmanager
def database_factory(admin_dsn: str) -> Iterator[DatabaseFactory]:
    """Create throwaway databases on demand; drop them all on exit."""
    created: list[str] = []

    def create() -> str:
        name = f"dataproof_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return psycopg.conninfo.make_conninfo(admin_dsn, dbname=name)

    try:
        yield create
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            for name in created:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


def alembic_config(change: str | None) -> Config:
    """The sample app's migrations, plus one candidate change set as the PR head would add it."""
    config = Config()
    config.set_main_option("script_location", str(SAMPLE_APP / "migrations"))
    config.set_main_option("path_separator", "os")
    locations = [SAMPLE_APP / "migrations" / "versions"]
    if change is not None:
        locations.append(CHANGES / change)
    config.set_main_option("version_locations", os.pathsep.join(map(str, locations)))
    return config


def migrate(dsn: str, change: str | None) -> None:
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn))
    try:
        with engine.connect() as connection:
            config = alembic_config(change)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def baseline(new_database: DatabaseFactory) -> str:
    """A fresh database at the reference revision, loaded with the published fixture."""
    dsn = new_database()
    migrate(dsn, change=None)
    with psycopg.connect(dsn) as connection:
        load_fixture(connection, build_fixture())
    return dsn


def candidate(new_database: DatabaseFactory, change: str) -> str:
    dsn = baseline(new_database)
    migrate(dsn, change)
    return dsn


def fetch(dsn: str, query: str) -> list[Row]:
    with psycopg.connect(dsn) as connection:
        return connection.execute(query).fetchall()


def scalar(dsn: str, query: str) -> Any:
    (row,) = fetch(dsn, query)
    return row[0]
