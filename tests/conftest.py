import json
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import docker
import psycopg
import pytest

from commerce_env import DatabaseFactory, database_factory
from dataproof_control import RunStore, migrate
from dataproof_runner.local_docker import LocalDockerBackend
from docker_env import AppRepo, build_runtime_image

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


@pytest.fixture
def new_database(postgres_dsn: str) -> Iterator[DatabaseFactory]:
    """Throwaway databases for one test, dropped afterwards."""
    with database_factory(postgres_dsn) as create:
        yield create


@pytest.fixture
def control_dsn(new_database: DatabaseFactory) -> str:
    """A fresh control database with every control migration applied."""
    dsn = new_database()
    with psycopg.connect(dsn) as connection:
        migrate(connection)
    return dsn


@pytest.fixture
def store(control_dsn: str) -> RunStore:
    return RunStore(control_dsn)


@pytest.fixture(scope="session")
def docker_client() -> Iterator[docker.DockerClient]:
    client = docker.from_env()
    yield client
    client.close()


@pytest.fixture(scope="session")
def runtime_image(docker_client: docker.DockerClient) -> str:
    """The trusted runtime image built from this checkout (cached by Docker between sessions)."""
    return build_runtime_image(docker_client)


@pytest.fixture
def backend(docker_client: docker.DockerClient) -> Iterator[LocalDockerBackend]:
    """A local Docker backend in a scope of its own; anything a test leaves behind is removed."""
    scoped = LocalDockerBackend(scope=f"t{uuid.uuid4().hex[:10]}", client=docker_client)
    yield scoped
    scoped.reap_expired(datetime.max.replace(tzinfo=UTC))


@pytest.fixture(scope="session")
def app_repo(tmp_path_factory: pytest.TempPathFactory) -> AppRepo:
    return AppRepo(tmp_path_factory.mktemp("app-repo") / "repo")
