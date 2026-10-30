"""Source staging from a trusted git clone (needs only git)."""

import io
import tarfile

import pytest

from dataproof_runner.source import archive_commit, changed_paths
from docker_env import SAMPLE_APP, AppRepo


def test_archive_is_exactly_the_commit_and_reproducible(app_repo: AppRepo) -> None:
    stray = app_repo.path / "untracked.txt"
    stray.write_text("not part of any commit", "utf-8")
    try:
        first = archive_commit(app_repo.path, app_repo.base)
        again = archive_commit(app_repo.path, app_repo.base)
    finally:
        stray.unlink()
    assert first.sha256 == again.sha256
    with tarfile.open(fileobj=io.BytesIO(first.data)) as archive:
        names = set(archive.getnames())
    assert f"{SAMPLE_APP}/alembic.ini" in names
    assert "untracked.txt" not in names


def test_changed_paths_lists_every_touched_path(app_repo: AppRepo) -> None:
    head = app_repo.commit(
        app_repo.base,
        {
            f"{SAMPLE_APP}/uv.lock": None,
            f"{SAMPLE_APP}/migrations/versions/0002_new.py": "revision = '0002'\n",
            f"{SAMPLE_APP}/README.md": "changed\n",
        },
        "Touch three paths",
    )
    assert changed_paths(app_repo.path, app_repo.base, head) == [
        f"{SAMPLE_APP}/README.md",
        f"{SAMPLE_APP}/migrations/versions/0002_new.py",
        f"{SAMPLE_APP}/uv.lock",
    ]


@pytest.mark.parametrize("sha", ["main", "HEAD~1", "abc123", "--output=/tmp/x"])
def test_only_full_commit_shas_are_accepted(app_repo: AppRepo, sha: str) -> None:
    with pytest.raises(ValueError, match="full commit SHA"):
        archive_commit(app_repo.path, sha)
    with pytest.raises(ValueError, match="full commit SHA"):
        changed_paths(app_repo.path, app_repo.base, sha)
