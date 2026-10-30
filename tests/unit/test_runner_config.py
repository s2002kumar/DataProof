"""Trusted project configuration, change policy and the local artifact store."""

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from dataproof_runner.artifacts import ArtifactIntegrityError, LocalArtifactStore
from dataproof_runner.project import ProjectConfig

IMAGE = "sha256:" + "a" * 64


def _config(**changes: object) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "repository": "example-org/commerce-demo",
            "runtime_image": IMAGE,
            "baseline": "commerce-v1",
            **changes,
        }
    )


def test_digest_is_stable_and_covers_every_setting() -> None:
    approvals = ["1" * 64, "2" * 64, "3" * 64]
    first = _config(approved_binding_digests=approvals)
    reordered = _config(approved_binding_digests=list(reversed(approvals)))
    assert first.digest() == reordered.digest()
    assert first.digest() != _config(approved_binding_digests=approvals[:2]).digest()
    assert (
        first.digest()
        != _config(approved_binding_digests=approvals, step_timeout_seconds=5).digest()
    )


@pytest.mark.parametrize(
    ("paths", "violations"),
    [
        (["sample-app/migrations/versions/0002_ledger.py", "dataproof/bindings/c1.toml"], []),
        (["sample-app/app/main.py", "sample-app/uv.lock"], ["sample-app/uv.lock"]),
        (["sample-app/pyproject.toml"], ["sample-app/pyproject.toml"]),
        (
            ["sample-app/web/package.json", "sample-app/web/pnpm-lock.yaml"],
            ["sample-app/web/package.json", "sample-app/web/pnpm-lock.yaml"],
        ),
        (["infra/docker/runtime/Dockerfile"], ["infra/docker/runtime/Dockerfile"]),
        (["sample-app/Dockerfile"], ["sample-app/Dockerfile"]),
        (["sample-app/requirements-dev.txt"], ["sample-app/requirements-dev.txt"]),
        ([".github/workflows/ci.yml"], [".github/workflows/ci.yml"]),
        (["README.md"], ["README.md"]),
        (["sample-app-evil/x.py"], ["sample-app-evil/x.py"]),
    ],
)
def test_policy_refuses_dependency_and_out_of_root_changes(
    paths: list[str], violations: list[str]
) -> None:
    assert _config().policy_violations(paths) == violations


@pytest.mark.parametrize(
    "changes",
    [
        {"runtime_image": "dataproof-runtime:dev"},
        {"baseline": "other"},
        {"allowed_change_roots": ["../outside/"]},
        {"allowed_change_roots": ["no-trailing-slash"]},
        {"alembic_config": "/etc/alembic.ini"},
        {"approved_binding_digests": ["not-a-digest"]},
        {"unexpected": True},
    ],
)
def test_invalid_configuration_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _config(**changes)


def test_artifacts_are_write_once_and_verified_on_read(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    run_id = uuid4()
    ref = store.put(run_id, "source-candidate.tar", b"archive")
    assert store.put(run_id, "source-candidate.tar", b"archive") == ref, "idempotent retry"
    assert store.get(ref.object_key, ref.sha256) == b"archive"
    with pytest.raises(ArtifactIntegrityError, match="different content"):
        store.put(run_id, "source-candidate.tar", b"replaced")

    (tmp_path / ref.object_key).write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="expected"):
        store.get(ref.object_key, ref.sha256)


@pytest.mark.parametrize("name", ["../escape.tar", "Upper.tar", "", "a/b.tar"])
def test_artifact_names_are_restricted(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="invalid artifact name"):
        LocalArtifactStore(tmp_path).put(uuid4(), name, b"x")


def test_artifact_keys_cannot_escape_the_store(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes the store"):
        LocalArtifactStore(tmp_path / "store").get("../outside", "0" * 64)
