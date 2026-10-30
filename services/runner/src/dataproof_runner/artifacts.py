"""Write-once run artifacts on local disk: the local evidence store for source archives.

Objects live under ``runs/<run id>/<name>``. Writing the same name twice is allowed only with the
same bytes, so a retried stage is idempotent and nothing can be silently replaced. Every read
re-verifies the SHA-256 recorded when the object was written.
"""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


class ArtifactIntegrityError(RuntimeError):
    """An object's bytes differ from the digest it was recorded with."""


@dataclass(frozen=True)
class ArtifactRef:
    object_key: str
    sha256: str
    size_bytes: int


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"object key escapes the store: {object_key!r}")
        return path

    def put(self, run_id: UUID, name: str, data: bytes) -> ArtifactRef:
        if not _NAME.match(name):
            raise ValueError(f"invalid artifact name: {name!r}")
        object_key = f"runs/{run_id}/{name}"
        ref = ArtifactRef(object_key, hashlib.sha256(data).hexdigest(), len(data))
        path = self._path(object_key)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != ref.sha256:
                raise ArtifactIntegrityError(f"{object_key} already exists with different content")
            return ref
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(data)
        os.replace(handle.name, path)
        return ref

    def get(self, object_key: str, sha256: str) -> bytes:
        data = self._path(object_key).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise ArtifactIntegrityError(f"{object_key} has SHA-256 {actual}, expected {sha256}")
        return data
