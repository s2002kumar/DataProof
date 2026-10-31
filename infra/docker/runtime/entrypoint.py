"""Trusted entrypoint of the DataProof runtime image (ADR-0004, "Source artifact delivery").

It runs as an unprivileged user before any pull-request code. It reads the source archive that
the trusted supervisor placed at ``/run/dataproof/source.tar``, verifies its SHA-256 against the
launch parameter ``DATAPROOF_SOURCE_SHA256``, unpacks it with path and link checks into
``/work/src``, and only then replaces itself with the requested step.

Steps:

    migrate --config <path to alembic.ini in the source> --target <revision|head>
    build-web --dir <path to the Next.js app in the source>

``build-web`` copies the verified frontend source into the image's frontend directory, next to
the dependencies baked from the lockfile (Turbopack compiles nothing outside its project root),
and runs ``next build`` offline. A source that ships its own ``node_modules`` is refused.

Exit codes: 64 bad arguments, 65 source digest mismatch (no pull-request code ran), 66 missing or
unsafe source archive (no pull-request code ran); otherwise the step's own exit code.

This file uses only the standard library and is baked into the image; the pull request cannot
change it.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

SOURCE = Path("/run/dataproof/source.tar")
WORKSPACE = Path("/work/src")
ALEMBIC = "/opt/venv/bin/alembic"
NODE = "/usr/local/bin/node"
WEB_ROOT = Path("/opt/web")
EX_USAGE, EX_DIGEST_MISMATCH, EX_SOURCE_INVALID = 64, 65, 66
_REVISION = re.compile(r"^(head|[A-Za-z0-9_]{1,64})$")


def _report(event: str, **fields: object) -> None:
    print(json.dumps({"dataproof": event, **fields}, sort_keys=True), file=sys.stderr, flush=True)


def _relative(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise argparse.ArgumentTypeError(f"not a relative source path: {path!r}")
    return candidate


def _revision(value: str) -> str:
    if not _REVISION.match(value):
        raise argparse.ArgumentTypeError(f"not a revision identifier: {value!r}")
    return value


def _verified_source() -> int:
    expected = os.environ.get("DATAPROOF_SOURCE_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        _report("SOURCE_INVALID", reason="missing or malformed DATAPROOF_SOURCE_SHA256")
        return EX_SOURCE_INVALID
    try:
        data = SOURCE.read_bytes()
    except OSError as error:
        _report("SOURCE_INVALID", reason=f"cannot read source archive: {error.strerror}")
        return EX_SOURCE_INVALID
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        _report("SOURCE_DIGEST_MISMATCH", expected=expected, actual=actual)
        return EX_DIGEST_MISMATCH
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            # The "data" filter refuses absolute paths, traversal, links that leave the
            # destination, device files and setuid bits.
            archive.extractall(WORKSPACE, filter="data")
    except (tarfile.TarError, OSError) as error:
        _report("SOURCE_INVALID", reason=f"unsafe or unreadable archive: {error}")
        return EX_SOURCE_INVALID
    _report("SOURCE_VERIFIED", sha256=actual)
    return 0


def _minimal_environment(**extra: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "NEXT_TELEMETRY_DISABLED", "PYTHONDONTWRITEBYTECODE"}
    }
    environment["HOME"] = "/tmp"  # noqa: S108 - a private tmpfs in the step container
    environment.update(extra)
    return environment


def _build_web(app: Path) -> int:
    if not (app / "package.json").is_file():
        _report("SOURCE_INVALID", reason="no package.json in the frontend directory")
        return EX_SOURCE_INVALID
    if (app / "node_modules").exists() or (app / "node_modules").is_symlink():
        _report("SOURCE_INVALID", reason="the source ships its own node_modules")
        return EX_SOURCE_INVALID
    shutil.copytree(app, WEB_ROOT, symlinks=True, dirs_exist_ok=True)
    os.chdir(WEB_ROOT)
    _report("STEP_STARTED", step="build-web")
    environment = _minimal_environment(NODE_ENV="production", NEXT_TELEMETRY_DISABLED="1")
    next_cli = str(WEB_ROOT / "node_modules" / "next" / "dist" / "bin" / "next")
    os.execve(NODE, [NODE, next_cli, "build"], environment)  # noqa: S606
    return 0  # pragma: no cover - execve does not return


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dataproof-step", exit_on_error=False)
    steps = parser.add_subparsers(dest="step", required=True)
    migrate = steps.add_parser("migrate", exit_on_error=False)
    migrate.add_argument("--config", type=_relative, required=True)
    migrate.add_argument("--target", type=_revision, required=True)
    build_web = steps.add_parser("build-web", exit_on_error=False)
    build_web.add_argument("--dir", type=_relative, required=True)
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit) as error:
        _report("USAGE", reason=str(error))
        return EX_USAGE

    status = _verified_source()
    if status != 0:
        return status

    if args.step == "build-web":
        return _build_web(WORKSPACE / args.dir)

    config = WORKSPACE / args.config
    if not config.is_file():
        _report("SOURCE_INVALID", reason=f"{args.config} is not a file in the source")
        return EX_SOURCE_INVALID
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"DATABASE_URL", "PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}
    }
    environment["HOME"] = "/tmp"  # noqa: S108 - a private tmpfs in the step container
    os.chdir(config.parent)
    _report("STEP_STARTED", step="migrate", target=args.target)
    os.execve(ALEMBIC, [ALEMBIC, "-c", str(config), "upgrade", args.target], environment)  # noqa: S606
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
