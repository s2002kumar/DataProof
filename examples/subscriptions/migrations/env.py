"""Alembic environment for the subscriptions example.

The database URL comes from ``DATABASE_URL`` (``postgresql+psycopg://...``), which is the only
thing a DataProof migration step receives. Revisions are plain SQL, so there is no metadata
target. Each revision runs in its own transaction, so ``alembic_version`` only records revisions
that committed.
"""

import os

from alembic import context
from sqlalchemy import Connection, create_engine


def _run(connection: Connection) -> None:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set DATABASE_URL (postgresql+psycopg://...) to run migrations")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline SQL generation is not supported; run against a database")
run_migrations_online()
