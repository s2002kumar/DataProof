"""The execution-backend interface (ADR-0006).

The supervisor creates, uses and destroys run environments only through this interface. An
environment is one role (reference or candidate) of one run: an isolated PostgreSQL database and
the network that bounded steps run on. Run state, leases, verification, evidence and reporting
never call a backend-specific API.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from psycopg.conninfo import make_conninfo

from dataproof_contracts import CleanupOutcome, Environment


@dataclass(frozen=True)
class ResourceLimits:
    """Caps applied to every container of an environment, unless a step asks for others."""

    memory_bytes: int = 512 * 1024 * 1024
    cpus: float = 1.0
    pids: int = 256
    database_storage_bytes: int = 512 * 1024 * 1024
    workspace_bytes: int = 256 * 1024 * 1024


#: What a Next.js production build of the sample frontend needs (ADR-0004: a measured gate).
FRONTEND_BUILD_LIMITS = ResourceLimits(memory_bytes=2 * 1024 * 1024 * 1024, cpus=2.0, pids=512)


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class DatabaseAccess:
    """How trusted services reach an environment's database. Never given to a step as-is.

    ``owner`` owns the application database (no superuser, role or database creation); steps
    migrate with it and the trusted supervisor loads the baseline with it. ``reader`` can only
    read, and its sessions default to read-only transactions; verification uses it. The
    PostgreSQL superuser's credentials never leave the database container.
    """

    host: str
    port: int
    dbname: str
    owner: Credentials
    reader: Credentials

    def conninfo(self, credentials: Credentials) -> str:
        return make_conninfo(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=credentials.user,
            password=credentials.password,
            connect_timeout=10,
        )


@dataclass(frozen=True)
class EnvironmentHandle:
    run_id: UUID
    role: Environment
    isolation_id: str
    database: DatabaseAccess
    expires_at: datetime
    variant: str = ""
    """Empty for the run's own environment of this role; for example ``restore`` for a fresh
    environment that a snapshot is restored into."""


@dataclass(frozen=True)
class StagedSource:
    """A source archive of one commit, as staged by the trusted broker, and its SHA-256.

    The digest proves the integrity of this artifact, not that a later fetch reproduces it.
    """

    commit_sha: str
    data: bytes = field(repr=False)
    sha256: str

    @classmethod
    def of(cls, commit_sha: str, data: bytes) -> "StagedSource":
        return cls(commit_sha=commit_sha, data=data, sha256=hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class StepResult:
    """A finished (or killed) step. ``output`` is redacted and truncated."""

    exit_code: int | None
    output: str
    duration_seconds: float
    timed_out: bool

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class CleanupReport:
    run_id: UUID
    outcome: CleanupOutcome
    removed: tuple[str, ...]


class ExecutionBackend(Protocol):
    """What the supervisor needs from any place that can run environments."""

    name: str

    @property
    def database_image(self) -> str:
        """The PostgreSQL image every environment runs, by digest."""
        ...

    def provision(
        self, run_id: UUID, role: Environment, *, expires_at: datetime, variant: str = ""
    ) -> EnvironmentHandle:
        """Create an isolated, empty, ready database environment for one role of a run."""
        ...

    def attach(
        self, run_id: UUID, role: Environment, *, variant: str = ""
    ) -> EnvironmentHandle | None:
        """The live environment of one role of a run, or None if it does not exist or is down.

        A worker that resumes a run after another worker's lease ended uses this to reach the
        environments that worker provisioned.
        """
        ...

    def run_step(
        self,
        environment: EnvironmentHandle,
        *,
        image: str,
        source: StagedSource,
        arguments: Sequence[str],
        timeout: timedelta,
        limits: ResourceLimits | None = None,
    ) -> StepResult:
        """Run one bounded step of the trusted runtime image against the staged source.

        ``limits`` overrides the backend's default caps for this step, for example to give a
        frontend build the memory it needs.
        """
        ...

    def dump_database(self, environment: EnvironmentHandle) -> bytes:
        """``pg_dump --format=custom`` of the environment's database, taken by trusted code."""
        ...

    def restore_database(self, environment: EnvironmentHandle, dump: bytes) -> None:
        """Restore a ``pg_dump`` archive into a fresh environment's empty database."""
        ...

    def destroy(
        self, run_id: UUID, *, role: Environment | None = None, variant: str | None = None
    ) -> CleanupReport:
        """Remove every resource of a run, or of one role (and variant) of it. Idempotent."""
        ...

    def reap_expired(self, now: datetime) -> list[CleanupReport]:
        """Remove every run whose environments outlived their expiry."""
        ...

    def resources(self, run_id: UUID | None = None) -> list[str]:
        """Names of the resources that exist for one run, or for every run."""
        ...
