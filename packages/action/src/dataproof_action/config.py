"""The repository-owned configuration of the Action, read from the base branch only (ADR-0007).

It names the Alembic setup, the fixture, where the protected oracles and the proposed bindings
live, and which binding digests a maintainer has approved. Because the Action reads it from the
base branch, a pull request cannot change what it is held to: an edit to this file, to an oracle
or to the fixture takes effect only once it is merged. Unknown fields are rejected, so a
misspelt key cannot silently relax a setting.
"""

import hashlib
import json
import tomllib
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, PositiveInt, ValidationError, model_validator

from dataproof_contracts import CandidateBinding, sha256_digest
from dataproof_contracts._base import Sha256Hex
from dataproof_runner.project import RelativeDir, RelativeFile

DEFAULT_CONFIG_PATH = "dataproof/config.toml"


class ConfigError(ValueError):
    """The configuration is missing, malformed or inconsistent; the Action cannot run."""


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AlembicSection(_Section):
    config: RelativeFile
    """``alembic.ini``, relative to the repository root. Its ``env.py`` must take the database
    URL from the ``DATABASE_URL`` environment variable: that is the only thing a migration step
    receives."""
    requirements: RelativeFile | None = None
    """A pinned requirements file for what ``env.py`` imports beyond Alembic, SQLAlchemy and
    psycopg, read from the base branch and installed into the migration image before any
    pull-request code runs. A pull request cannot add a dependency to its own run."""
    timeout_seconds: PositiveInt = 600
    """How long one migration step may run before it is killed."""


class FixtureSection(_Section):
    sql: RelativeFile
    """A SQL file loaded, from the base branch, into both databases after the base branch's
    migrations. Its SHA-256 is the run's dataset digest."""


class ContractsSection(_Section):
    oracle_dir: RelativeDir = "dataproof/oracles/"
    """Protected oracles (TOML), read from the base branch only."""
    binding_dir: RelativeDir = "dataproof/bindings/"
    """Candidate bindings (TOML) proposed by the pull request, read from its head commit."""
    approved_bindings: tuple[Sha256Hex, ...] = ()
    """Digests of bindings a maintainer approved, on the base branch. A binding whose digest is
    not listed leaves every check that depends on it ``NOT_RUN: AWAITING_BINDING_REVIEW``, so
    the run is ``INCOMPLETE`` and the job fails: approval is a reviewed change to this file, and
    a pull request cannot approve its own binding."""
    rejected_bindings: tuple[Sha256Hex, ...] = ()

    @model_validator(mode="after")
    def _distinct_decisions(self) -> Self:
        both = set(self.approved_bindings) & set(self.rejected_bindings)
        if both:
            raise ValueError(f"bindings both approved and rejected: {sorted(both)}")
        return self


class PolicySection(_Section):
    forks: Literal["run", "refuse"] = "run"
    """Whether a pull request from a fork is verified. Its migration runs under the same
    isolation as any other; ``refuse`` reports ``NOT_EXECUTED: FORK_PULL_REQUEST`` instead."""


class ActionConfig(_Section):
    version: Literal[1]
    alembic: AlembicSection
    fixture: FixtureSection
    contracts: ContractsSection = ContractsSection()
    policy: PolicySection = PolicySection()

    def digest(self) -> str:
        """SHA-256 of the canonical configuration, recorded with every run."""
        return sha256_digest(self)

    def trusted_files(self, config_path: str) -> tuple[str, ...]:
        """Files the Action reads from the base branch, never from the pull request."""
        files = [config_path, self.fixture.sql]
        if self.alembic.requirements is not None:
            files.append(self.alembic.requirements)
        return tuple(files)


def binding_approval_digest(contract_id: str, binding: CandidateBinding) -> str:
    """The digest a maintainer approves: the binding's content, not the commit it rides on.

    The GitHub App binds an approval to a head commit, because its approval starts a run on that
    commit. An approval on the base branch outlives commits: it says this exact binding text,
    for this contract, is an acceptable way to read the candidate schema. Any edit to the text
    changes the digest and needs a new approval. The commit and path are recorded with the run.
    """
    canonical = json.dumps(
        {
            "contract_id": contract_id,
            "query_sql": binding.query_sql,
            "api_route": binding.api_route,
            "column_mapping": dict(sorted(binding.column_mapping.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_config(text: str) -> ActionConfig:
    try:
        return ActionConfig.model_validate(tomllib.loads(text))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"the configuration is not valid TOML: {error}") from error
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in error.errors()
        )
        raise ConfigError(f"the configuration is invalid: {problems}") from error
