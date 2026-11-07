"""The execution-backend interface (ADR-0006).

The supervisor creates, uses and destroys run environments only through this interface. An
environment is one role (reference or candidate) of one run: an isolated PostgreSQL database and
the network that bounded steps run on. Run state, leases, verification, evidence and reporting
never call a backend-specific API.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
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


#: A step variable's name: what a POSIX shell would accept, upper case, bounded.
STEP_VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
#: Names a step's own environment already sets; a configuration cannot replace them.
RESERVED_STEP_VARIABLES = frozenset(
    {"DATABASE_URL", "PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
)
#: Placeholders a step variable's value may name: the environment's own database, as the step
#: reaches it. Nothing else is substituted, and no other brace is accepted.
STEP_PLACEHOLDERS = frozenset({"host", "port", "user", "password", "dbname", "database_url"})
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
MAX_STEP_VARIABLES = 32
MAX_STEP_VALUE = 1024


def step_variable_problems(variables: Mapping[str, str]) -> list[str]:
    """Why a set of step variables is unacceptable; empty if it is acceptable.

    The same rules hold wherever the set is declared (a trusted configuration) and wherever it
    is applied (the trusted entrypoint checks them again).
    """
    problems = []
    if len(variables) > MAX_STEP_VARIABLES:
        problems.append(f"at most {MAX_STEP_VARIABLES} variables")
    for name, value in variables.items():
        if not STEP_VARIABLE.match(name):
            problems.append(f"{name!r} is not an upper-case variable name")
        elif name in RESERVED_STEP_VARIABLES or name.startswith("DATAPROOF_"):
            problems.append(f"{name} is set by DataProof itself")
        if len(value) > MAX_STEP_VALUE:
            problems.append(f"{name} is longer than {MAX_STEP_VALUE} characters")
        for placeholder in _PLACEHOLDER.findall(value):
            if placeholder not in STEP_PLACEHOLDERS:
                known = ", ".join(f"{{{p}}}" for p in sorted(STEP_PLACEHOLDERS))
                problems.append(f"{name} names {{{placeholder}}}; the placeholders are {known}")
        if value.count("{") != value.count("}"):
            problems.append(f"{name} has an unmatched brace")
    return problems


def resolve_step_variables(
    variables: Mapping[str, str], coordinates: Mapping[str, str]
) -> dict[str, str]:
    """Each value with its placeholders replaced by the step's database coordinates."""
    if problems := step_variable_problems(variables):
        raise ValueError("; ".join(problems))
    return {
        name: _PLACEHOLDER.sub(lambda match: coordinates[match.group(1)], value)
        for name, value in variables.items()
    }


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
class PreviewAccess:
    """How a reviewer reaches one environment's live preview: a URL and its run-scoped token.

    ``login_url`` sets the token as an HTTP-only cookie; without it every route but the health
    check answers 401.
    """

    url: str
    token: str = field(repr=False)

    @property
    def login_url(self) -> str:
        return f"{self.url}/__dataproof/login?token={self.token}"


@dataclass(frozen=True)
class PreviewResult:
    """A preview that started serving, or why it did not; with what it cost to get there."""

    access: PreviewAccess | None
    failure: str | None
    """``build`` (the frontend did not build), ``start`` (a server exited or never answered) or
    ``None``."""
    output: str
    commit_sha: str
    ready_seconds: float
    build_seconds: float | None
    peak_memory_bytes: int
    cpu_seconds: float

    @property
    def ready(self) -> bool:
        return self.access is not None


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
        variables: Mapping[str, str] | None = None,
    ) -> StepResult:
        """Run one bounded step of the trusted runtime image against the staged source.

        ``variables`` adds environment variables to the step beyond ``DATABASE_URL``, from a
        trusted configuration only, with the placeholders ``STEP_PLACEHOLDERS`` resolved to the
        environment's own database as the step reaches it (``resolve_step_variables``). The
        trusted entrypoint applies them to a migration and checks them again.

        ``limits`` overrides the backend's default caps for this step, for example to give a
        frontend build the memory it needs.
        """
        ...

    def start_preview(
        self,
        environment: EnvironmentHandle,
        *,
        image: str,
        source: StagedSource,
        arguments: Sequence[str],
        timeout: timedelta,
        limits: ResourceLimits | None = None,
    ) -> PreviewResult:
        """Build and serve the source's application, reachable only through a trusted edge.

        The application reads the database with the read-only role. The edge publishes one URL
        that requires the preview token.
        """
        ...

    def preview_access(self, environment: EnvironmentHandle) -> PreviewAccess | None:
        """The access to a running preview, for a reviewer or a check; None if it is not up."""
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
