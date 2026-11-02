"""Build the demo repository: the sample app on ``main`` and two pull-request branches.

    python scripts/demo/build_demo_repo.py <empty directory>

``main`` holds the sample app (every file git tracks under ``sample-app/``) and the protected
commerce oracles in ``dataproof/oracles/``. Each branch adds one ledger change set as its pull
request would: the Alembic revision, the ledger version of the API and frontend, and the
change set's bindings.

* ``ledger-backfill``: the grouped-join change (F1 in the design documents). It migrates
  successfully and multiplies refund 1002 from 49.99 to 99.98.
* ``ledger-backfill-direct``: the direct copy, which preserves every event.

Branch names and commit messages are neutral, as a real pull request's would be.
Push the result to the allowlisted demo repository, for example with
``gh repo create <owner>/<name> --private --source <directory> --push``, then push the branches.
"""

import argparse
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SAMPLE_APP = "sample-app"
SCENARIO = REPO / "scenarios" / "commerce"
CHANGES = SCENARIO / "changes"
LEDGER_APP = SCENARIO / "ledger-app"
BRANCHES = {"ledger-backfill": "ledger-backfill-07", "ledger-backfill-direct": "ledger-backfill-01"}
MESSAGE = "Move legacy payment events into a per-order ledger"
README = """# Commerce Orders & Refunds (DataProof demo)

The sample application that DataProof reviews: a FastAPI service, a Next.js frontend and
PostgreSQL migrations under `sample-app/`. `dataproof/oracles/` holds the protected data and API
contracts; pull requests propose `dataproof/bindings/` for their own schema.
"""


def git(directory: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git binary
        [  # noqa: S607
            "git",
            "-c",
            "user.name=DataProof Demo",
            "-c",
            "user.email=demo@dataproof.invalid",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(directory),
            *arguments,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def write(directory: Path, files: Mapping[str, str]) -> None:
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8", newline="\n")


def base_files() -> dict[str, str]:
    tracked = git(REPO, "ls-files", SAMPLE_APP).split()
    files = {name: (REPO / name).read_text("utf-8") for name in tracked}
    for oracle in sorted((SCENARIO / "oracles").glob("*.toml")):
        files[f"dataproof/oracles/{oracle.name}"] = oracle.read_text("utf-8")
    files["README.md"] = README
    return files


def change_files(change: str) -> dict[str, str]:
    (revision,) = (CHANGES / change).glob("*.py")
    files = {
        f"{SAMPLE_APP}/migrations/versions/{revision.name}": revision.read_text("utf-8"),
        f"{SAMPLE_APP}/app/payments.py": (LEDGER_APP / "payments.py").read_text("utf-8"),
        f"{SAMPLE_APP}/web/lib/payments.ts": (LEDGER_APP / "web" / "lib" / "payments.ts").read_text(
            "utf-8"
        ),
    }
    for binding in sorted((CHANGES / change / "bindings").glob("*.toml")):
        files[f"dataproof/bindings/{binding.name}"] = binding.read_text("utf-8")
    return files


def build(directory: Path) -> dict[str, str]:
    """Build the repository; the head commit of every branch."""
    if directory.exists() and any(directory.iterdir()):
        raise SystemExit(f"{directory} is not empty")
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q", "-b", "main")
    write(directory, base_files())
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "Commerce sample app and its protected contracts")
    heads = {"main": git(directory, "rev-parse", "HEAD")}
    for branch, change in BRANCHES.items():
        git(directory, "checkout", "-q", "-b", branch, "main")
        write(directory, change_files(change))
        git(directory, "add", "-A")
        git(directory, "commit", "-q", "-m", MESSAGE)
        heads[branch] = git(directory, "rev-parse", "HEAD")
    git(directory, "checkout", "-q", "main")
    return heads


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    for branch, sha in build(args.directory).items():
        print(f"{branch}: {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
