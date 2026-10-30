"""Base model, constrained string types and canonical hashing shared by all contracts."""

import hashlib
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class Contract(BaseModel):
    """Immutable contract model that rejects unknown fields.

    Rejecting unknown fields matters for trust boundaries: an untrusted document cannot smuggle
    extra keys (for example a tolerance inside a candidate binding) past validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


def canonical_json(model: BaseModel) -> bytes:
    """Serialize a model as canonical JSON: sorted keys, no insignificant whitespace, UTF-8."""
    payload = model.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_digest(model: BaseModel) -> str:
    """SHA-256 hex digest of a model's canonical JSON."""
    return hashlib.sha256(canonical_json(model)).hexdigest()


Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""Lowercase hex SHA-256 digest."""

GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
"""Full lowercase hex Git commit SHA; abbreviated SHAs are ambiguous and rejected."""

OciDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
"""Container image digest."""

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
"""Stable identifier for checks, contracts, findings and evidence records."""

RepositoryName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
]
"""GitHub repository in ``owner/name`` form."""

GitHubLogin = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")]
"""GitHub user login."""

NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
