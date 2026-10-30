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

from commerce_scenario import PUBLISHED_DIGEST, build_fixture, load_fixture
from dataproof_contracts import CandidateBinding, DataContract
from dataproof_verifier import (
    QueryLimits,
    Verification,
    assemble_contract,
    load_binding,
    load_oracle,
    verify_contract,
)

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


C1, C2, C3 = (
    "commerce.c1_event_preservation",
    "commerce.c2_refund_totals",
    "commerce.c3_capture_totals",
)
BASE_SHA, HEAD_SHA = "b" * 40, "c" * 40


def contracts(
    change: str,
    *,
    approve: bool = True,
    reject: bool = False,
    overrides: dict[str, CandidateBinding] | None = None,
) -> list[DataContract]:
    """The commerce contracts with the change set's bindings, reviewed as requested."""
    result = []
    for oracle_path in sorted(ORACLES.glob("*.toml")):
        spec, oracle = load_oracle(oracle_path, source_path=oracle_path.name, base_sha=BASE_SHA)
        binding_path = CHANGES / change / "bindings" / oracle_path.name
        contract_id, binding = load_binding(
            binding_path, source_path=binding_path.name, head_sha=HEAD_SHA
        )
        assert contract_id == spec.contract_id
        binding = (overrides or {}).get(contract_id, binding)
        digests = {binding.digest()}
        result.append(
            assemble_contract(
                spec,
                oracle,
                binding,
                approved_digests=digests if approve and not reject else (),
                rejected_digests=digests if reject else (),
            )
        )
    return result


def verify(
    contract_list: list[DataContract],
    reference: str,
    candidate_dsn: str,
    limits: QueryLimits | None = None,
) -> dict[str, Verification]:
    with (
        psycopg.connect(reference, autocommit=True) as ref,
        psycopg.connect(candidate_dsn, autocommit=True) as cand,
    ):
        return {
            c.contract_id: verify_contract(c, ref, cand, PUBLISHED_DIGEST, limits or QueryLimits())
            for c in contract_list
        }


def override(
    contract_id: str, query_sql: str, **mapping_changes: Any
) -> dict[str, CandidateBinding]:
    name = contract_id.split(".")[1] + ".toml"
    _, binding = load_binding(
        CHANGES / SAFE_CHANGE / "bindings" / name, source_path=name, head_sha=HEAD_SHA
    )
    mapping = {**binding.column_mapping, **mapping_changes}
    mapping = {k: v for k, v in mapping.items() if v is not None}
    return {
        contract_id: binding.model_copy(update={"query_sql": query_sql, "column_mapping": mapping})
    }
