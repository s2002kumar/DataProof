"""Forward-only, checksummed SQL migrations for the control database.

Migrations are the ``NNNN_name.sql`` files shipped in this package, applied in order inside one
transaction under an advisory lock. The SHA-256 of every applied file is recorded; if a recorded
migration's file later changes, :func:`migrate` refuses to run rather than leave databases with
different schemas under the same version.
"""

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import psycopg

# Arbitrary constant naming the control-schema advisory lock.
_LOCK_KEY = 0x44_50_43_54_52_4C  # "DPCTRL"
_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


class MigrationDriftError(RuntimeError):
    """An applied migration no longer matches the file that ships with this package."""


def migrations() -> list[Migration]:
    found: list[Migration] = []
    for entry in files("dataproof_control").joinpath("migrations").iterdir():
        match = _FILENAME.match(entry.name)
        if match is None:
            continue
        found.append(
            Migration(int(match["version"]), match["name"], entry.read_text(encoding="utf-8"))
        )
    found.sort(key=lambda m: m.version)
    if [m.version for m in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("control migrations must be numbered 0001, 0002, ... without gaps")
    return found


def migrate(connection: psycopg.Connection[Any]) -> list[int]:
    """Apply pending migrations; return the versions applied by this call."""
    applied_now: list[int] = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS control_schema_migrations (
                version    integer PRIMARY KEY,
                name       text NOT NULL,
                sha256     text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT statement_timestamp()
            )
            """
        )
        cursor = connection.execute("SELECT version, sha256 FROM control_schema_migrations")
        recorded = {int(row[0]): str(row[1]) for row in cursor.fetchall()}
        for migration in migrations():
            if migration.version in recorded:
                if recorded[migration.version] != migration.sha256:
                    raise MigrationDriftError(
                        f"control migration {migration.version:04d}_{migration.name} changed "
                        "after it was applied; add a new migration instead"
                    )
                continue
            # A migration file holds several statements, so it is sent without parameters
            # (simple query protocol). It is trusted code shipped in this package.
            connection.execute(migration.sql.encode("utf-8"))
            connection.execute(
                "INSERT INTO control_schema_migrations (version, name, sha256) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.sha256),
            )
            applied_now.append(migration.version)
    return applied_now
