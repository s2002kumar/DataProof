"""Trusted project configuration and the change policy evaluated in ``ANALYZING``.

This is the operator's configuration for one supported application repository: never read from
the pull request. A run's inputs pin its digest (``RunInputs.config_digest``), and a run whose
configuration no longer matches is not executed under the new one. Approving or rejecting a
binding changes the digest, so the run an approval starts never collides with the original.
"""

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_serializer, field_validator

from dataproof_contracts import sha256_digest
from dataproof_contracts._base import RepositoryName, Sha256Hex

_DIGEST_PINNED = re.compile(r"^(sha256:[0-9a-f]{64}|[^@\s]+@sha256:[0-9a-f]{64})$")

#: File names that define the runtime or its dependencies. A pull request that changes one is
#: not executed until a trusted image is approved (ADR-0004).
DEPENDENCY_FILE_PATTERNS = (
    "pyproject.toml",
    "uv.lock",
    "requirements*.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "yarn.lock",
    ".npmrc",
    "Dockerfile",
    "*.Dockerfile",
    "Containerfile",
    ".dockerignore",
)

# Relative POSIX paths whose segments cannot be "." or "..".
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
RelativeDir = Annotated[str, Field(pattern=rf"^{_SEGMENT}(/{_SEGMENT})*/$")]
RelativeFile = Annotated[str, Field(pattern=rf"^{_SEGMENT}(/{_SEGMENT})*$")]


class ProjectConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: RepositoryName
    runtime_image: str
    """The trusted runtime image, by image ID or digest."""
    baseline: Literal["commerce-v1"]
    alembic_config: RelativeFile = "sample-app/alembic.ini"
    allowed_change_roots: tuple[RelativeDir, ...] = ("sample-app/", "dataproof/")
    dependency_file_patterns: tuple[str, ...] = DEPENDENCY_FILE_PATTERNS
    oracle_dir: RelativeDir = "dataproof/oracles/"
    """Read from the base commit only: a pull request cannot change the oracles it is held to."""
    binding_dir: RelativeDir = "dataproof/bindings/"
    """Read from the head commit: bindings are proposed by the pull request (ADR-0005)."""
    approved_binding_digests: frozenset[Sha256Hex] = frozenset()
    rejected_binding_digests: frozenset[Sha256Hex] = frozenset()
    step_timeout_seconds: PositiveInt = 600

    @field_validator("runtime_image")
    @classmethod
    def _pinned(cls, value: str) -> str:
        if not _DIGEST_PINNED.match(value):
            raise ValueError("runtime_image must be an image ID or a digest reference")
        return value

    @field_serializer("approved_binding_digests", "rejected_binding_digests")
    def _sorted(self, digests: frozenset[str]) -> list[str]:
        # Set iteration order varies between processes; the digest must not.
        return sorted(digests)

    def digest(self) -> str:
        """SHA-256 of the canonical configuration; what ``RunInputs.config_digest`` pins."""
        return sha256_digest(self)

    def policy_violations(self, changed_paths: Iterable[str]) -> list[str]:
        """Changed paths that make a pull request ineligible for execution.

        A path is ineligible if it is outside every allowed change root, or if its file name
        matches a runtime or dependency file pattern anywhere in the tree.
        """
        violations = []
        for path in changed_paths:
            name = PurePosixPath(path).name
            outside = not any(path.startswith(root) for root in self.allowed_change_roots)
            dependency = any(fnmatchcase(name, p) for p in self.dependency_file_patterns)
            if outside or dependency:
                violations.append(path)
        return sorted(violations)
