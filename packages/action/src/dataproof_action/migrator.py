"""The migration image: built in the job from trusted inputs, before any pull-request code runs.

The image holds Python, Alembic, SQLAlchemy and psycopg at pinned versions, the repository's own
pinned requirements for ``env.py`` if the base branch's configuration names a file, and the same
trusted entrypoint as the GitHub App's runtime image (``infra/docker/runtime/entrypoint.py``):
it verifies the staged source archive's SHA-256 as an unprivileged user and only then runs
``alembic upgrade``. The build context is a temporary directory holding exactly these files; the
workspace is never in it. The image is referenced by its ID, so a step cannot be given a
different image than the one that was built.
"""

import shutil
import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[4] / "infra" / "docker" / "runtime" / "entrypoint.py"
BASE_IMAGE = (
    "python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159"
)
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.13.6"
    "@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0"
)
PINNED_REQUIREMENTS = "alembic==1.20.1\nsqlalchemy==2.1.6\npsycopg[binary]==3.3.6\n"
"""What every migration image has, at the versions this repository is locked to."""

DOCKERFILE = f"""\
FROM {UV_IMAGE} AS uv
FROM {BASE_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY requirements.txt /build/requirements.txt
RUN uv venv /opt/venv \\
    && uv pip install --python /opt/venv/bin/python --no-cache -r /build/requirements.txt
COPY entrypoint.py /opt/dataproof/entrypoint.py
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin runner \\
    && install -d -m 0755 /run/dataproof /work
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/opt/venv/bin/python", "-I", "/opt/dataproof/entrypoint.py"]
"""


class BuildError(RuntimeError):
    pass


def write_context(directory: Path, *, requirements: str | None) -> None:
    """Lay out the build context: Dockerfile, requirements and the trusted entrypoint."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(DOCKERFILE, "utf-8", newline="\n")
    extra = requirements.strip() + "\n" if requirements and requirements.strip() else ""
    (directory / "requirements.txt").write_text(PINNED_REQUIREMENTS + extra, "utf-8", newline="\n")
    shutil.copyfile(ENTRYPOINT, directory / "entrypoint.py")


def build_image(directory: Path, *, requirements: str | None, timeout: float = 900) -> str:
    """Build the migration image from a fresh context and return its image ID (``sha256:``).

    The ID is read from ``--iidfile``, which the classic builder and BuildKit both write, rather
    than parsed from the build's output.
    """
    write_context(directory, requirements=requirements)
    docker = shutil.which("docker")
    if docker is None:
        raise BuildError("docker is not on PATH; the Action needs a Docker daemon on the runner")
    iidfile = directory / "image-id"
    command = [docker, "build", "--iidfile", str(iidfile), "--file", "Dockerfile", "."]
    built = subprocess.run(  # noqa: S603 - the resolved docker binary, fixed arguments
        command,
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if built.returncode != 0:
        raise BuildError(
            f"the migration image did not build ({' '.join(command)}): "
            f"{built.stderr.strip()[-2000:]}"
        )
    image_id = iidfile.read_text("utf-8").strip() if iidfile.is_file() else ""
    if not image_id.startswith("sha256:"):
        raise BuildError(f"docker build wrote no image ID: {built.stderr.strip()[-300:]}")
    return image_id


def remove_image(image_id: str) -> None:
    """Best effort: a GitHub-hosted runner is discarded anyway; a self-hosted one is not."""
    subprocess.run(  # noqa: S603 - fixed docker binary
        ["docker", "image", "rm", "--force", image_id],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=120,
    )
