"""Database access: one read-only connection per request, from ``DATABASE_URL``."""

import os
from collections.abc import Iterator

import psycopg
from psycopg.rows import DictRow, dict_row

Connection = psycopg.Connection[DictRow]


def _conninfo() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set DATABASE_URL to the application database")
    # The migrations use SQLAlchemy URLs (postgresql+psycopg://...); psycopg wants plain ones.
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def connection() -> Iterator[Connection]:
    """FastAPI dependency: the API only reads, so its sessions are read-only."""
    with psycopg.connect(_conninfo(), row_factory=dict_row) as conn:
        conn.read_only = True
        yield conn
