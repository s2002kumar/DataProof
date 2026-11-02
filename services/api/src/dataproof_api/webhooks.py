"""GitHub webhook signatures and the parts of a pull request event that intake uses.

A delivery is trusted only if ``X-Hub-Signature-256`` is the HMAC-SHA256 of the exact raw body
under the shared webhook secret, compared in constant time. Nothing in the body is looked at
before that. The event models read only the fields intake needs and ignore the rest.
"""

import hashlib
import hmac
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints

from dataproof_contracts._base import GitSha, RepositoryName

SIGNATURE_PREFIX = "sha256="

#: A GitHub account login, including apps acting as users (``dependabot[bot]``).
AccountLogin = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")
]


def sign(secret: bytes, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` value GitHub sends for this body."""
    return SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    if not secret:
        raise ValueError("a webhook secret is required")
    if header is None or not header.startswith(SIGNATURE_PREFIX):
        return False
    return hmac.compare_digest(sign(secret, body).encode(), header.encode())


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Repository(_Model):
    full_name: RepositoryName


class User(_Model):
    login: AccountLogin


class Installation(_Model):
    id: PositiveInt


class Ref(_Model):
    sha: GitSha
    repo: Repository | None
    """None when the repository the branch lives in has been deleted."""


class PullRequest(_Model):
    number: PositiveInt
    user: User
    base: Ref
    head: Ref


class PullRequestEvent(_Model):
    action: str
    pull_request: PullRequest
    repository: Repository
    installation: Installation | None = None
    sender: User
