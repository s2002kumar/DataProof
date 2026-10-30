"""Trusted entrypoint of the DataProof runtime image (ADR-0004, "Source artifact delivery").

It runs as an unprivileged user before any pull-request code. It reads the source archive that
the trusted supervisor placed at ``/run/dataproof/source.tar``, verifies its SHA-256 against the
launch parameter ``DATAPROOF_SOURCE_SHA256``, unpacks it with path and link checks into
``/work/src``, and only then replaces itself with the requested step.

Steps:

    migrate --config <path to alembic.ini in the source> --target <revision|head>

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
import sys
import tarfile
from pathlib import Path, PurePosixPath

SOURCE = Path("/run/dataproof/source.tar")
WORKSPACE = Path("/work/src")
ALEMBIC = "/opt/venv/bin/alembic"
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dataproof-step", exit_on_error=False)
    steps = parser.add_subparsers(dest="step", required=True)
    migrate = steps.add_parser("migrate", exit_on_error=False)
    migrate.add_argument("--config", type=_relative, required=True)
    migrate.add_argument("--target", type=_revision, required=True)
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit) as error:
        _report("USAGE", reason=str(error))
        return EX_USAGE

    status = _verified_source()
    if status != 0:
        return status

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
