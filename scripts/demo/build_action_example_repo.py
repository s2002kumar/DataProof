"""Build the public example repository for the DataProof Action.

    python scripts/demo/build_action_example_repo.py <empty directory> --action-ref <commit sha>

``main`` holds the subscriptions example (``examples/subscriptions``: the Alembic project, the
configuration, the fixture and the oracles), a workflow that calls the Action pinned to
``--action-ref``, and a CODEOWNERS file for the workflow and the DataProof directory. Three
branches stand ready as pull requests:

* ``money-columns``: the correct cents-to-money migration, with its binding. Its first run is
  ``INCOMPLETE``: the binding awaits approval.
* ``money-columns-truncated``: the same migration with integer division. Same binding.
* ``approve-money-columns-binding``: the maintainer's approval, a one-line change to
  ``dataproof/config.toml`` on top of ``main``. Once merged, re-running the two pull requests
  gives ``CHECKED`` and ``BLOCKED``.

Push with ``gh repo create <owner>/<name> --public --source <directory> --push``, then push the
three branches and open the pull requests. Every result on GitHub is then a genuine run.
"""

import argparse
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "subscriptions"
CHANGES = EXAMPLE / "changes"
BRANCHES = {"money-columns": "money-columns", "money-columns-truncated": "money-columns-truncated"}
MESSAGE = "Store invoice amounts as exact money instead of integer cents"
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
UPLOAD = "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f # v6.0.0"

README = (EXAMPLE / "scripts" / "REPOSITORY_README.md").read_text("utf-8")

CODEOWNERS = """# The workflow that calls DataProof, and what DataProof trusts, need owner review.
/.github/workflows/dataproof.yml @{owner}
/.github/CODEOWNERS @{owner}
/dataproof/ @{owner}
"""

WORKFLOW = """name: dataproof

on:
  pull_request:

permissions:
  contents: read

jobs:
  verify:
    name: dataproof
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: {checkout}
        with:
          persist-credentials: false
      - name: DataProof
        id: dataproof
        uses: s2002kumar/DataProof@{action_ref}
      - uses: {upload}
        if: always()
        with:
          name: dataproof-report
          path: dataproof-report
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


def base_files(*, owner: str, action_ref: str) -> dict[str, str]:
    files = {}
    for path in sorted(EXAMPLE.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(EXAMPLE).as_posix()
        if relative.startswith(("changes/", "scripts/")) or relative == "README.md":
            continue
        if "__pycache__" in path.parts:
            continue
        files[relative] = path.read_text("utf-8")
    files["README.md"] = README
    files[".github/CODEOWNERS"] = CODEOWNERS.format(owner=owner)
    files[".github/workflows/dataproof.yml"] = WORKFLOW.format(
        checkout=CHECKOUT, upload=UPLOAD, action_ref=action_ref
    )
    files[".gitignore"] = "__pycache__/\ndataproof-report/\n"
    return files


def change_files(change: str) -> dict[str, str]:
    (revision,) = (CHANGES / change).glob("*.py")
    files = {f"migrations/versions/{revision.name}": revision.read_text("utf-8")}
    for binding in sorted((CHANGES / change / "bindings").glob("*.toml")):
        files[f"dataproof/bindings/{binding.name}"] = binding.read_text("utf-8")
    return files


def approval_digest() -> str:
    from dataproof_action.config import binding_approval_digest
    from dataproof_verifier import parse_binding

    text = (CHANGES / "money-columns" / "bindings" / "invoice_amounts.toml").read_text("utf-8")
    contract_id, binding = parse_binding(text, source_path="x", head_sha="0" * 40)
    return binding_approval_digest(contract_id, binding)


def build(directory: Path, *, owner: str, action_ref: str) -> dict[str, str]:
    """Build the repository; the head commit of every branch."""
    if directory.exists() and any(directory.iterdir()):
        raise SystemExit(f"{directory} is not empty")
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q", "-b", "main")
    write(directory, base_files(owner=owner, action_ref=action_ref))
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "Subscriptions example with its DataProof contracts")
    heads = {"main": git(directory, "rev-parse", "HEAD")}
    for branch, change in BRANCHES.items():
        git(directory, "checkout", "-q", "-b", branch, "main")
        write(directory, change_files(change))
        git(directory, "add", "-A")
        git(directory, "commit", "-q", "-m", MESSAGE)
        heads[branch] = git(directory, "rev-parse", "HEAD")
    git(directory, "checkout", "-q", "-b", "approve-money-columns-binding", "main")
    config = directory / "dataproof" / "config.toml"
    text = config.read_text("utf-8").replace(
        "approved_bindings = []", f'approved_bindings = ["{approval_digest()}"]'
    )
    config.write_text(text, "utf-8", newline="\n")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "Approve the money-columns binding")
    heads["approve-money-columns-binding"] = git(directory, "rev-parse", "HEAD")
    git(directory, "checkout", "-q", "main")
    return heads


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--owner", default="s2002kumar", help="GitHub login for CODEOWNERS")
    parser.add_argument("--action-ref", required=True, help="commit SHA of DataProof to pin")
    args = parser.parse_args(argv)
    for branch, sha in build(args.directory, owner=args.owner, action_ref=args.action_ref).items():
        print(f"{branch}: {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
