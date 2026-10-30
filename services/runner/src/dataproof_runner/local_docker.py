"""Local Docker execution backend: a trusted-development boundary (ADR-0006).

Per run and role it creates:

* ``<prefix>-int``: an *internal* network (no route to the internet, the host or other runs).
  Steps attach only to it and reach their own database as ``postgres``.
* ``<prefix>-ctl``: a network whose only member is the database; it exists so that the
  database port can be published on the host's loopback interface for trusted services.
* ``<prefix>-db``: PostgreSQL from a digest-pinned image, as the ``postgres`` user, with every
  capability dropped, ``no-new-privileges``, resource limits and data on tmpfs. An init script
  creates the owner and reader roles and the application database; the superuser password is
  generated here, passed only to that container and never stored. The owner and reader
  passwords are kept in that container's configuration, which only Docker API users (the trusted
  operator) can read, so that a worker resuming a run can :meth:`attach` to it.
* ``<prefix>-step-<id>``: short-lived step containers from the trusted runtime image (by image ID
  or digest only), as UID 10001, with every capability dropped and resource limits. The source
  archive is copied in before start, and the image's entrypoint verifies its digest.

Every resource carries labels for its scope, run, role and expiry, which is all that cleanup and
the expiry reconciler need: neither depends on the control plane being up.
"""

import io
import re
import secrets
import tarfile
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import docker
import psycopg
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from docker.models.networks import Network
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ReadTimeout

from dataproof_contracts import CleanupOutcome, Environment
from dataproof_runner.backend import (
    CleanupReport,
    Credentials,
    DatabaseAccess,
    EnvironmentHandle,
    ResourceLimits,
    StagedSource,
    StepResult,
)

POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b"
)
LABEL = "dev.dataproof"
_MANAGED, _SCOPE, _RUN, _ROLE, _EXPIRES, _KIND = (
    f"{LABEL}.{name}" for name in ("managed", "scope", "run", "role", "expires-at", "kind")
)
_SCOPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
_STEP_USER = "10001:10001"
_DATABASE = "app"
_OWNER, _READER = "dataproof_owner", "dataproof_reader"
_OWNER_PASSWORD, _READER_PASSWORD = "DATAPROOF_OWNER_PASSWORD", "DATAPROOF_READER_PASSWORD"
_MAX_OUTPUT = 64 * 1024
_DIGEST_PINNED = re.compile(r"^(sha256:[0-9a-f]{64}|[^@\s]+@sha256:[0-9a-f]{64})$")


class BackendError(RuntimeError):
    pass


def _tar(name: str, data: bytes, mode: int = 0o444) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = mode
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _init_sql(owner: Credentials, reader: Credentials) -> bytes:
    # Passwords come from secrets.token_urlsafe: [A-Za-z0-9_-] only, safe inside quotes.
    return f"""
CREATE ROLE {_OWNER} LOGIN PASSWORD '{owner.password}'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE {_READER} LOGIN PASSWORD '{reader.password}'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT pg_read_all_data TO {_READER};
ALTER ROLE {_READER} SET default_transaction_read_only = on;
CREATE DATABASE {_DATABASE} OWNER {_OWNER};
REVOKE ALL ON DATABASE {_DATABASE} FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE {_DATABASE} TO {_READER};
\\connect {_DATABASE}
ALTER SCHEMA public OWNER TO {_OWNER};
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO {_READER};
""".encode()


def _redact(text: str, secrets_: Iterable[str]) -> str:
    for secret in secrets_:
        text = text.replace(secret, "[REDACTED]")
    return text


class LocalDockerBackend:
    name = "local-docker"

    def __init__(
        self,
        *,
        scope: str = "local",
        postgres_image: str = POSTGRES_IMAGE,
        limits: ResourceLimits | None = None,
        client: docker.DockerClient | None = None,
        ready_timeout: timedelta = timedelta(seconds=90),
    ) -> None:
        if not _SCOPE_PATTERN.match(scope):
            raise ValueError("scope must be 1-24 lowercase letters, digits or hyphens")
        if not _DIGEST_PINNED.match(postgres_image):
            raise ValueError("the PostgreSQL image must be pinned by digest")
        self.scope = scope
        self._postgres_image = postgres_image
        self._limits = limits or ResourceLimits()
        self._client = client or docker.from_env()
        self._ready_timeout = ready_timeout

    # Naming and labels -------------------------------------------------------------------------

    def _prefix(self, run_id: UUID, role: Environment) -> str:
        return f"dp-{self.scope}-{run_id.hex[:12]}-{role.value.lower()}"

    def _labels(
        self, run_id: UUID, role: Environment, kind: str, expires_at: datetime
    ) -> dict[str, str]:
        return {
            _MANAGED: "true",
            _SCOPE: self.scope,
            _RUN: str(run_id),
            _ROLE: role.value,
            _EXPIRES: expires_at.astimezone(UTC).isoformat(),
            _KIND: kind,
        }

    def _filters(self, run_id: UUID | None, role: Environment | None = None) -> dict[str, Any]:
        labels = [f"{_MANAGED}=true", f"{_SCOPE}={self.scope}"]
        if run_id is not None:
            labels.append(f"{_RUN}={run_id}")
        if role is not None:
            labels.append(f"{_ROLE}={role.value}")
        return {"label": labels}

    def _limits_kwargs(self) -> dict[str, Any]:
        return {
            "mem_limit": self._limits.memory_bytes,
            "memswap_limit": self._limits.memory_bytes,
            "nano_cpus": int(self._limits.cpus * 1_000_000_000),
            "pids_limit": self._limits.pids,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "privileged": False,
        }

    # Provisioning ------------------------------------------------------------------------------

    def provision(
        self, run_id: UUID, role: Environment, *, expires_at: datetime
    ) -> EnvironmentHandle:
        prefix = self._prefix(run_id, role)
        internal = self._client.networks.create(
            f"{prefix}-int",
            driver="bridge",
            internal=True,
            labels=self._labels(run_id, role, "network", expires_at),
        )
        self._client.networks.create(
            f"{prefix}-ctl",
            driver="bridge",
            labels=self._labels(run_id, role, "network", expires_at),
        )
        owner = Credentials(_OWNER, secrets.token_urlsafe(24))
        reader = Credentials(_READER, secrets.token_urlsafe(24))
        database = self._client.containers.create(
            self._postgres_image,
            name=f"{prefix}-db",
            user="postgres",
            network=f"{prefix}-ctl",
            # docker-py maps a None host port to an ephemeral one; the stubs omit that case.
            ports={"5432/tcp": ("127.0.0.1", None)},  # type: ignore[dict-item]
            environment={
                "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
                "PGDATA": "/var/lib/postgresql/data/pgdata",
                _OWNER_PASSWORD: owner.password,
                _READER_PASSWORD: reader.password,
            },
            tmpfs={
                "/var/lib/postgresql/data": (
                    f"rw,mode=1777,size={self._limits.database_storage_bytes}"
                ),
            },
            labels=self._labels(run_id, role, "database", expires_at),
            **self._limits_kwargs(),
        )
        if not database.put_archive(
            "/docker-entrypoint-initdb.d", _tar("00-dataproof.sql", _init_sql(owner, reader))
        ):
            raise BackendError("could not stage the database init script")
        internal.connect(database, aliases=["postgres"])
        database.start()
        access = DatabaseAccess(
            host="127.0.0.1",
            port=self._published_port(database),
            dbname=_DATABASE,
            owner=owner,
            reader=reader,
        )
        self._wait_until_ready(database, access)
        return EnvironmentHandle(
            run_id=run_id,
            role=role,
            isolation_id=prefix,
            database=access,
            expires_at=expires_at,
        )

    def attach(self, run_id: UUID, role: Environment) -> EnvironmentHandle | None:
        prefix = self._prefix(run_id, role)
        try:
            database = self._client.containers.get(f"{prefix}-db")
        except NotFound:
            return None
        labels = database.attrs["Config"].get("Labels") or {}
        if labels.get(_SCOPE) != self.scope or database.status != "running":
            return None
        settings = dict(
            item.split("=", 1) for item in database.attrs["Config"].get("Env") or [] if "=" in item
        )
        access = DatabaseAccess(
            host="127.0.0.1",
            port=self._published_port(database),
            dbname=_DATABASE,
            owner=Credentials(_OWNER, settings[_OWNER_PASSWORD]),
            reader=Credentials(_READER, settings[_READER_PASSWORD]),
        )
        return EnvironmentHandle(
            run_id=run_id,
            role=role,
            isolation_id=prefix,
            database=access,
            expires_at=datetime.fromisoformat(labels[_EXPIRES]),
        )

    def _published_port(self, container: Container) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            container.reload()
            bindings = (container.ports or {}).get("5432/tcp") or []
            for binding in bindings:
                if binding.get("HostIp") in ("127.0.0.1", "") and binding.get("HostPort"):
                    return int(binding["HostPort"])
            time.sleep(0.2)
        raise BackendError(f"{container.name} has no loopback port for PostgreSQL")

    def _wait_until_ready(self, container: Container, access: DatabaseAccess) -> None:
        # The image's init phase runs a socket-only server; TCP succeeds only once the init
        # script has created the roles and the final server is up.
        deadline = time.monotonic() + self._ready_timeout.total_seconds()
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            container.reload()
            if container.status in ("exited", "dead"):
                raise BackendError(f"{container.name} stopped while starting")
            try:
                with psycopg.connect(access.conninfo(access.owner), connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                return
            except psycopg.OperationalError as error:
                last_error = error
                time.sleep(0.5)
        raise BackendError(f"{container.name} did not become ready: {last_error}")

    # Steps -------------------------------------------------------------------------------------

    def run_step(
        self,
        environment: EnvironmentHandle,
        *,
        image: str,
        source: StagedSource,
        arguments: Sequence[str],
        timeout: timedelta,
    ) -> StepResult:
        if not _DIGEST_PINNED.match(image):
            raise ValueError("step images must be referenced by image ID or digest")
        owner = environment.database.owner
        database_url = (
            f"postgresql+psycopg://{owner.user}:{owner.password}@postgres:5432/{_DATABASE}"
        )
        container = self._client.containers.create(
            image,
            command=list(arguments),
            name=f"{environment.isolation_id}-step-{uuid4().hex[:8]}",
            user=_STEP_USER,
            network=f"{environment.isolation_id}-int",
            environment={"DATAPROOF_SOURCE_SHA256": source.sha256, "DATABASE_URL": database_url},
            tmpfs={
                "/work": f"rw,mode=1777,size={self._limits.workspace_bytes}",
                "/tmp": "rw,mode=1777,size=67108864",  # noqa: S108 - container-private tmpfs
            },
            labels=self._labels(
                environment.run_id, environment.role, "step", environment.expires_at
            ),
            **self._limits_kwargs(),
        )
        try:
            if not container.put_archive("/run/dataproof", _tar("source.tar", source.data)):
                raise BackendError("could not stage the source archive")
            started = time.monotonic()
            container.start()
            timed_out, exit_code = False, None
            try:
                status = container.wait(timeout=timeout.total_seconds())
                exit_code = int(status["StatusCode"])
            except (ReadTimeout, RequestsConnectionError):
                timed_out = True
                container.kill()
                container.wait(timeout=30)
            duration = time.monotonic() - started
            raw = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        finally:
            container.remove(force=True)
        output = _redact(raw, [owner.password, environment.database.reader.password])
        if len(output) > _MAX_OUTPUT:
            output = "[truncated]\n" + output[-_MAX_OUTPUT:]
        return StepResult(
            exit_code=exit_code, output=output, duration_seconds=duration, timed_out=timed_out
        )

    # Cleanup -----------------------------------------------------------------------------------

    def _containers(self, run_id: UUID | None, role: Environment | None = None) -> list[Container]:
        filters = self._filters(run_id, role)
        return list(self._client.containers.list(all=True, filters=filters))

    def _networks(self, run_id: UUID | None, role: Environment | None = None) -> list[Network]:
        return list(self._client.networks.list(filters=self._filters(run_id, role)))

    def resources(self, run_id: UUID | None = None) -> list[str]:
        names = [str(c.name) for c in self._containers(run_id)]
        names += [str(n.name) for n in self._networks(run_id)]
        return sorted(names)

    def destroy(self, run_id: UUID, *, role: Environment | None = None) -> CleanupReport:
        removed: list[str] = []
        for container in self._containers(run_id, role):
            try:
                container.remove(force=True, v=True)
                removed.append(str(container.name))
            except NotFound:
                continue
        for network in self._networks(run_id, role):
            try:
                network.remove()
                removed.append(str(network.name))
            except NotFound:
                continue
            except APIError as error:
                raise BackendError(f"could not remove {network.name}: {error}") from error
        outcome = CleanupOutcome.SUCCEEDED if removed else CleanupOutcome.ALREADY_GONE
        return CleanupReport(run_id=run_id, outcome=outcome, removed=tuple(sorted(removed)))

    def reap_expired(self, now: datetime) -> list[CleanupReport]:
        expiries: dict[UUID, datetime] = {}
        resources: list[Container | Network] = [*self._containers(None), *self._networks(None)]
        for resource in resources:
            labels = resource.attrs.get("Labels") or resource.attrs.get("Config", {}).get("Labels")
            labels = labels or {}
            try:
                run_id = UUID(labels[_RUN])
                expires = datetime.fromisoformat(labels[_EXPIRES])
            except (KeyError, ValueError):
                continue
            expiries[run_id] = min(expires, expiries.get(run_id, expires))
        return [
            self.destroy(run_id) for run_id, expires in sorted(expiries.items()) if expires <= now
        ]
