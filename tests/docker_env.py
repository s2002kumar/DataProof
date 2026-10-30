"""Helpers for tests of the local Docker backend: the runtime image and an application repository.

The application repository is a throwaway git repository whose base commit is the sample app as
tracked in this repository; tests add head commits on top of it the way a pull request would.
"""

import subprocess
from collections.abc import Mapping
from pathlib import Path

import docker

REPO = Path(__file__).resolve().parents[1]
RUNTIME_DOCKERFILE = "infra/docker/runtime/Dockerfile"
SAMPLE_APP = "sample-app"
BASE_REVISION = "0001"
CHANGES = REPO / "scenarios" / "commerce" / "changes"


def build_runtime_image(client: docker.DockerClient) -> str:
    """Build the trusted runtime image from this checkout and return its image ID."""
    image, _ = client.images.build(path=str(REPO), dockerfile=RUNTIME_DOCKERFILE, rm=True)
    image_id = image.id
    assert image_id is not None
    return image_id


class AppRepo:
    """A git repository standing in for a configured application repository."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")
        tracked = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", SAMPLE_APP],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.split()
        self.base = self.commit(
            None, {name: (REPO / name).read_text("utf-8") for name in tracked}, "Sample app"
        )

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "user.name=DataProof Tests",
                "-c",
                "user.email=tests@dataproof.invalid",
                "-c",
                "core.autocrlf=false",
                "-C",
                str(self.path),
                *arguments,
            ],
            capture_output=True,
            check=True,
            text=True,
        )
        return completed.stdout.strip()

    def commit(self, parent: str | None, files: Mapping[str, str | None], message: str) -> str:
        """Commit on top of ``parent``: write files with text, delete files mapped to None."""
        if parent is not None:
            self._git("checkout", "-q", "--detach", parent)
        for name, text in files.items():
            target = self.path / name
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        self._git("add", "-A", "--", *files)
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def with_revision(self, filename: str, text: str) -> str:
        """A head commit that adds one Alembic revision to the sample app."""
        return self.commit(
            self.base, {f"{SAMPLE_APP}/migrations/versions/{filename}": text}, f"Add {filename}"
        )

    def with_change_set(self, change: str) -> str:
        """A head commit that adds a commerce change set's revision, as its pull request would."""
        (revision,) = (CHANGES / change).glob("*.py")
        return self.with_revision(revision.name, revision.read_text("utf-8"))


def revision(revision_id: str, body: str, *, imports: str = "") -> str:
    """Source of a minimal Alembic revision on top of the reference schema."""
    return f'''"""Test revision {revision_id}."""

{imports}
from alembic import op
import sqlalchemy as sa

revision = "{revision_id}"
down_revision = "{BASE_REVISION}"
branch_labels = None
depends_on = None


def upgrade() -> None:
{body}


def downgrade() -> None:
    pass
'''


def probe_revision(targets: Mapping[str, tuple[str, int]]) -> str:
    """A hostile revision that probes its sandbox and records what it could reach."""
    body = f"""    targets = {dict(targets)!r}

    def attempt(host, port):
        try:
            socket.create_connection((host, port), timeout=3).close()
            return "connected"
        except OSError as error:
            return type(error).__name__

    status = {{}}
    for line in open("/proc/self/status").read().splitlines():
        key, _, value = line.partition(":")
        status[key] = value.strip()
    result = {{
        "network": {{name: attempt(*target) for name, target in targets.items()}},
        "uid": os.getuid(),
        "cap_eff": status["CapEff"],
        "no_new_privs": status["NoNewPrivs"],
        "environment": sorted(os.environ),
        "docker_socket": os.path.exists("/var/run/docker.sock"),
    }}
    print("leaking", os.environ["DATABASE_URL"])
    op.execute(sa.text("CREATE TABLE sandbox_probe (result jsonb NOT NULL)"))
    op.execute(
        sa.text("INSERT INTO sandbox_probe VALUES (CAST(:result AS jsonb))").bindparams(
            result=json.dumps(result)
        )
    )"""  # noqa: S608
    return revision("9a0b1c2d3e4f", body, imports="import json\nimport os\nimport socket")
