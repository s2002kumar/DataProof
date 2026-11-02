"""Reference and candidate databases for the benchmark, at the database tier.

Each candidate is a fresh database migrated to the base schema with the sample app's revisions,
loaded with the published fixture, then migrated by the case's revision, as a run's candidate
environment is. The reference is loaded the same way without a case. The verifier then reads
both exactly as ``VERIFYING`` does.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine

import commerce_scenario
from commerce_scenario import build_fixture, load_fixture

REPOSITORY = Path(commerce_scenario.__file__).resolve().parents[4]
SAMPLE_APP = REPOSITORY / "sample-app"

DatabaseFactory = Callable[[], str]


@contextmanager
def databases(admin_dsn: str) -> Iterator[DatabaseFactory]:
    """Create throwaway databases on demand; drop them all on exit."""
    created: list[str] = []

    def create() -> str:
        name = f"benchmark_{uuid.uuid4().hex[:12]}"
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


def migrate(dsn: str, change_dir: Path | None) -> None:
    config = Config()
    config.set_main_option("script_location", str(SAMPLE_APP / "migrations"))
    config.set_main_option("path_separator", "os")
    locations = [SAMPLE_APP / "migrations" / "versions"]
    if change_dir is not None:
        locations.append(change_dir)
    config.set_main_option("version_locations", os.pathsep.join(map(str, locations)))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def reference(new_database: DatabaseFactory) -> str:
    """A fresh database at the base revision, loaded with the published fixture."""
    dsn = new_database()
    migrate(dsn, None)
    with psycopg.connect(dsn) as connection:
        load_fixture(connection, build_fixture())
    return dsn


def candidate(new_database: DatabaseFactory, change_dir: Path) -> str:
    dsn = reference(new_database)
    migrate(dsn, change_dir)
    return dsn
