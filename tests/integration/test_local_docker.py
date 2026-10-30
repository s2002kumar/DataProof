"""The local Docker backend: real containers, real PostgreSQL, and hostile pull-request code.

Every isolation claim in ADR-0006 and THREAT_MODEL ("Local execution backend") has a negative
test here, made from inside a step by a migration that tries to break out.
"""

import io
import tarfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import docker
import psycopg
import pytest

from dataproof_contracts import CleanupOutcome, Environment
from dataproof_runner.backend import EnvironmentHandle, StagedSource
from dataproof_runner.local_docker import LocalDockerBackend
from dataproof_runner.source import archive_commit
from docker_env import BASE_REVISION, SAMPLE_APP, AppRepo, probe_revision, revision

pytestmark = [pytest.mark.integration, pytest.mark.docker]

STEP_TIMEOUT = timedelta(minutes=2)
MIGRATE = ["migrate", "--config", f"{SAMPLE_APP}/alembic.ini", "--target", "head"]
ALLOWED_STEP_ENVIRONMENT = {
    "DATABASE_URL",
    "HOME",
    "LANG",
    "PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUNBUFFERED",
}


def _later(minutes: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _query(handle: EnvironmentHandle, statement: str, *, reader: bool = False) -> list[Any]:
    access = handle.database
    credentials = access.reader if reader else access.owner
    with psycopg.connect(access.conninfo(credentials)) as conn:
        return conn.execute(statement).fetchall()


def _tables(handle: EnvironmentHandle) -> set[str]:
    rows = _query(handle, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row[0] for row in rows}


@pytest.fixture
def environment(backend: LocalDockerBackend) -> Iterator[EnvironmentHandle]:
    handle = backend.provision(uuid4(), Environment.CANDIDATE, expires_at=_later())
    yield handle
    backend.destroy(handle.run_id)


# Provisioning ----------------------------------------------------------------------------------


def test_database_roles_are_least_privileged(environment: EnvironmentHandle) -> None:
    for role in (environment.database.owner, environment.database.reader):
        with psycopg.connect(environment.database.conninfo(role)) as conn:
            flags = conn.execute(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
        assert flags == (False, False, False, False, False), role.user

    owner = environment.database.conninfo(environment.database.owner)
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute("CREATE TABLE notes (id integer PRIMARY KEY)")
        conn.execute("INSERT INTO notes VALUES (1)")
        for forbidden in (
            "COPY (SELECT 1) TO PROGRAM 'id'",
            "SELECT pg_read_file('/etc/passwd')",
            "CREATE EXTENSION dblink",
            "CREATE ROLE intruder SUPERUSER",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(forbidden)

    assert _query(environment, "SELECT id FROM notes", reader=True) == [(1,)]
    reader = environment.database.conninfo(environment.database.reader)
    with psycopg.connect(reader) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("INSERT INTO notes VALUES (2)")


def test_handles_never_carry_or_print_the_superuser_secret(
    environment: EnvironmentHandle,
) -> None:
    text = repr(environment)
    assert environment.database.owner.password not in text
    assert environment.database.reader.password not in text
    assert {role.user for role in (environment.database.owner, environment.database.reader)} == {
        "dataproof_owner",
        "dataproof_reader",
    }


def test_containers_and_networks_are_hardened(
    environment: EnvironmentHandle, docker_client: docker.DockerClient
) -> None:
    database = docker_client.containers.get(f"{environment.isolation_id}-db")
    host = database.attrs["HostConfig"]
    assert host["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["Privileged"] is False
    assert host["Memory"] > 0
    assert host["NanoCpus"] > 0
    assert host["PidsLimit"] > 0
    assert database.attrs["Config"]["User"] == "postgres"
    bindings = host["PortBindings"]["5432/tcp"]
    assert [binding["HostIp"] for binding in bindings] == ["127.0.0.1"]
    assert "POSTGRES_PASSWORD" in " ".join(database.attrs["Config"]["Env"])

    internal = docker_client.networks.get(f"{environment.isolation_id}-int")
    control = docker_client.networks.get(f"{environment.isolation_id}-ctl")
    assert internal.attrs["Internal"] is True
    assert set(control.attrs["Containers"]) == {database.id}


# Steps -----------------------------------------------------------------------------------------


def test_base_migration_runs_inside_a_step(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    source = archive_commit(app_repo.path, app_repo.base)
    result = backend.run_step(
        environment, image=runtime_image, source=source, arguments=MIGRATE, timeout=STEP_TIMEOUT
    )
    assert result.succeeded, result.output
    assert f'"sha256": "{source.sha256}"' in result.output
    assert _query(environment, "SELECT version_num FROM alembic_version") == [(BASE_REVISION,)]
    assert {"customers", "orders", "order_items", "legacy_payment_events"} <= _tables(environment)


def test_hostile_migration_cannot_reach_anything_but_its_own_database(
    backend: LocalDockerBackend, runtime_image: str, app_repo: AppRepo
) -> None:
    run_id = uuid4()
    reference = backend.provision(run_id, Environment.REFERENCE, expires_at=_later())
    candidate = backend.provision(run_id, Environment.CANDIDATE, expires_at=_later())
    head = app_repo.with_revision(
        "9a0b1c2d3e4f_probe.py",
        probe_revision(
            {
                "own_database": ("postgres", 5432),
                "internet_by_address": ("1.1.1.1", 443),
                "internet_by_name": ("example.com", 443),
                "host_gateway": ("host.docker.internal", reference.database.port),
                "host_loopback": ("127.0.0.1", reference.database.port),
                "reference_database": (f"{reference.isolation_id}-db", 5432),
            }
        ),
    )
    result = backend.run_step(
        candidate,
        image=runtime_image,
        source=archive_commit(app_repo.path, head),
        arguments=MIGRATE,
        timeout=STEP_TIMEOUT,
    )
    assert result.succeeded, result.output

    ((probe,),) = _query(candidate, "SELECT result FROM sandbox_probe")
    network = probe["network"]
    assert network.pop("own_database") == "connected", "positive control"
    assert all(outcome != "connected" for outcome in network.values()), network
    assert probe["uid"] == 10001
    assert probe["cap_eff"] == "0000000000000000"
    assert probe["no_new_privs"] == "1"
    assert probe["docker_socket"] is False
    assert set(probe["environment"]) <= ALLOWED_STEP_ENVIRONMENT
    assert "DATAPROOF_SOURCE_SHA256" not in probe["environment"]

    assert "leaking" in result.output
    assert candidate.database.owner.password not in result.output
    assert "[REDACTED]" in result.output
    assert "sandbox_probe" not in _tables(reference)


def test_a_source_digest_mismatch_runs_no_pull_request_code(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    head = app_repo.with_revision(
        "9a0b1c2d3e4f_probe.py", probe_revision({"own_database": ("postgres", 5432)})
    )
    genuine = archive_commit(app_repo.path, head)
    tampered = StagedSource(
        commit_sha=genuine.commit_sha, data=genuine.data + b"\0" * 512, sha256=genuine.sha256
    )
    result = backend.run_step(
        environment, image=runtime_image, source=tampered, arguments=MIGRATE, timeout=STEP_TIMEOUT
    )
    assert result.exit_code == 65
    assert "SOURCE_DIGEST_MISMATCH" in result.output
    assert "STEP_STARTED" not in result.output
    assert _tables(environment) == set(), "no migration, so no pull-request code, ran"


def test_an_archive_that_escapes_its_directory_is_refused(
    backend: LocalDockerBackend, environment: EnvironmentHandle, runtime_image: str
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        payload = b"print('escaped')\n"
        info = tarfile.TarInfo("../escape.py")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    hostile = StagedSource.of("0" * 40, buffer.getvalue())
    result = backend.run_step(
        environment, image=runtime_image, source=hostile, arguments=MIGRATE, timeout=STEP_TIMEOUT
    )
    assert result.exit_code == 66
    assert "SOURCE_INVALID" in result.output


def test_a_failing_migration_reports_its_error_and_commits_nothing_of_itself(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    head = app_repo.with_revision(
        "7c8d9e0f1a2b_broken.py",
        revision(
            "7c8d9e0f1a2b",
            '    op.execute("CREATE TABLE half_done (id integer)")\n    op.execute("SELECT 1 / 0")',
        ),
    )
    result = backend.run_step(
        environment,
        image=runtime_image,
        source=archive_commit(app_repo.path, head),
        arguments=MIGRATE,
        timeout=STEP_TIMEOUT,
    )
    assert not result.succeeded
    assert result.exit_code not in (None, 0)
    assert "division by zero" in result.output
    assert _query(environment, "SELECT version_num FROM alembic_version") == [(BASE_REVISION,)]
    assert "half_done" not in _tables(environment)


def test_a_step_that_overruns_its_timeout_is_killed(
    backend: LocalDockerBackend,
    environment: EnvironmentHandle,
    runtime_image: str,
    app_repo: AppRepo,
) -> None:
    head = app_repo.with_revision(
        "5e6f7a8b9c0d_slow.py",
        revision("5e6f7a8b9c0d", "    time.sleep(120)", imports="import time"),
    )
    result = backend.run_step(
        environment,
        image=runtime_image,
        source=archive_commit(app_repo.path, head),
        arguments=MIGRATE,
        timeout=timedelta(seconds=8),
    )
    assert result.timed_out
    assert result.exit_code is None
    assert result.duration_seconds < 60
    assert not any("-step-" in name for name in backend.resources(environment.run_id))


def test_steps_require_a_digest_pinned_image(
    backend: LocalDockerBackend, environment: EnvironmentHandle, app_repo: AppRepo
) -> None:
    with pytest.raises(ValueError, match="image ID or digest"):
        backend.run_step(
            environment,
            image="dataproof-runtime:dev",
            source=archive_commit(app_repo.path, app_repo.base),
            arguments=MIGRATE,
            timeout=STEP_TIMEOUT,
        )
    with pytest.raises(ValueError, match="pinned by digest"):
        LocalDockerBackend(scope="tags", postgres_image="postgres:17-alpine")


# Cleanup ---------------------------------------------------------------------------------------


def test_destroy_removes_every_resource_and_is_idempotent(backend: LocalDockerBackend) -> None:
    run_id = uuid4()
    for role in Environment:
        backend.provision(run_id, role, expires_at=_later())
    other = backend.provision(uuid4(), Environment.CANDIDATE, expires_at=_later())
    assert len(backend.resources(run_id)) == 6  # per role: database, internal and control network

    report = backend.destroy(run_id)
    assert report.outcome is CleanupOutcome.SUCCEEDED
    assert len(report.removed) == 6
    assert backend.resources(run_id) == []
    assert backend.destroy(run_id).outcome is CleanupOutcome.ALREADY_GONE
    assert len(backend.resources(other.run_id)) == 3, "other runs are untouched"


def test_the_reaper_removes_only_expired_runs(backend: LocalDockerBackend) -> None:
    expired = backend.provision(
        uuid4(), Environment.CANDIDATE, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    live = backend.provision(uuid4(), Environment.CANDIDATE, expires_at=_later())
    reports = backend.reap_expired(datetime.now(UTC))
    assert [report.run_id for report in reports] == [expired.run_id]
    assert backend.resources(expired.run_id) == []
    assert len(backend.resources(live.run_id)) == 3


def test_scopes_do_not_see_each_other(
    backend: LocalDockerBackend, docker_client: docker.DockerClient
) -> None:
    mine = backend.provision(uuid4(), Environment.CANDIDATE, expires_at=_later())
    neighbour = LocalDockerBackend(scope=f"{backend.scope}-x", client=docker_client)
    assert neighbour.resources() == []
    assert neighbour.reap_expired(datetime.max.replace(tzinfo=UTC)) == []
    assert len(backend.resources(mine.run_id)) == 3


def test_run_labels_name_the_run(backend: LocalDockerBackend) -> None:
    run_id = UUID("6f1d2c3e-0a4b-4c5d-8e9f-0123456789ab")
    handle = backend.provision(run_id, Environment.REFERENCE, expires_at=_later())
    assert handle.isolation_id == f"dp-{backend.scope}-6f1d2c3e0a4b-reference"
    assert set(backend.resources(run_id)) == {
        f"{handle.isolation_id}-db",
        f"{handle.isolation_id}-int",
        f"{handle.isolation_id}-ctl",
    }


def test_attach_recovers_a_working_handle_and_roles_can_be_destroyed_alone(
    backend: LocalDockerBackend,
) -> None:
    run_id = uuid4()
    provisioned = {
        role: backend.provision(run_id, role, expires_at=_later()) for role in Environment
    }
    attached = backend.attach(run_id, Environment.CANDIDATE)
    assert attached == provisioned[Environment.CANDIDATE]
    assert _query(attached, "SELECT current_user") == [("dataproof_owner",)]
    assert backend.attach(uuid4(), Environment.CANDIDATE) is None

    report = backend.destroy(run_id, role=Environment.CANDIDATE)
    assert len(report.removed) == 3
    assert backend.attach(run_id, Environment.CANDIDATE) is None
    assert backend.attach(run_id, Environment.REFERENCE) == provisioned[Environment.REFERENCE]


def test_attach_ignores_environments_of_another_scope(
    backend: LocalDockerBackend, docker_client: docker.DockerClient
) -> None:
    handle = backend.provision(uuid4(), Environment.CANDIDATE, expires_at=_later())
    neighbour = LocalDockerBackend(scope=f"{backend.scope}-y", client=docker_client)
    assert neighbour.attach(handle.run_id, Environment.CANDIDATE) is None
