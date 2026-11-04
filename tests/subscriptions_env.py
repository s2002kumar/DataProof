"""The subscriptions example as a repository the Action verifies, with pull requests on it.

``upstream`` stands for the repository on GitHub: ``main`` holds the example (its Alembic
project, configuration, fixture and oracles), and head commits add a change set as a pull
request would. ``workspace`` is a clone of it, as ``actions/checkout`` leaves one, with
``origin`` pointing at upstream, so the Action fetches the base branch and the head commit the
way it does in a job.
"""

import json
import os
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataproof_action import DEFAULT_CONFIG_PATH, Inputs, run
from dataproof_action.config import binding_approval_digest
from dataproof_verifier import parse_binding

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "subscriptions"
CHANGES = EXAMPLE / "changes"
INVOICE_AMOUNTS = "subscriptions.invoice_amounts"
CANARY = {
    "GITHUB_TOKEN": "ghs_CANARY_TOKEN_0123456789",
    "ACTIONS_RUNTIME_TOKEN": "CANARY_RUNTIME_TOKEN",
    "ANTHROPIC_API_KEY": "sk-ant-CANARY",
    "DATAPROOF_CANARY_SECRET": "CANARY_SECRET_VALUE",
}
"""Secrets a job may hold. None may reach a migration, a report or a summary."""


def _git(directory: Path, *arguments: str) -> str:
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
            str(directory),
            *arguments,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def example_files() -> dict[str, str]:
    """Every file of the example a repository would hold (not the change sets or scripts)."""
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
    return files


def change_files(change: str, *, bindings: bool = True) -> dict[str, str]:
    """A change set's revision and bindings, at the paths its pull request would add."""
    (revision,) = (CHANGES / change).glob("*.py")
    files = {f"migrations/versions/{revision.name}": revision.read_text("utf-8")}
    if bindings:
        for binding in sorted((CHANGES / change / "bindings").glob("*.toml")):
            files[f"dataproof/bindings/{binding.name}"] = binding.read_text("utf-8")
    return files


def approval_digest(change: str = "money-columns") -> str:
    """The digest a maintainer adds to ``approved_bindings`` for a change set's binding."""
    text = (CHANGES / change / "bindings" / "invoice_amounts.toml").read_text("utf-8")
    contract_id, binding = parse_binding(text, source_path="x", head_sha="0" * 40)
    return binding_approval_digest(contract_id, binding)


def revision(revision_id: str, body: str, *, imports: str = "") -> str:
    """Source of an Alembic revision on top of the example's base schema."""
    return f'''"""Test revision {revision_id}."""

{imports}
from alembic import op

revision = "{revision_id}"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
{body}


def downgrade() -> None:
    pass
'''


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    report: dict[str, Any]
    summary: str
    output: dict[str, str]

    def coverage(self, check_id: str) -> dict[str, Any]:
        rows: list[dict[str, Any]] = [
            c for c in self.report["coverage"] if c["check_id"] == check_id
        ]
        (row,) = rows
        return row


class SubscriptionsRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.upstream = root / "upstream"
        self.workspace = root / "workspace"
        self.upstream.mkdir(parents=True)
        _git(self.upstream, "init", "-q", "-b", "main")
        _git(self.upstream, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.base = self.commit(None, example_files(), "Subscriptions example", branch="main")
        _git(root, "clone", "-q", str(self.upstream), str(self.workspace))
        self._runs = 0

    def commit(
        self,
        parent: str | None,
        files: Mapping[str, str | None],
        message: str,
        *,
        branch: str | None = None,
    ) -> str:
        """Commit on top of ``parent`` (detached) or on ``branch``; None deletes a file."""
        if branch is not None and parent is not None:
            _git(self.upstream, "checkout", "-q", branch)
        elif parent is not None:
            _git(self.upstream, "checkout", "-q", "--detach", parent)
        for name, text in files.items():
            target = self.upstream / name
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        _git(self.upstream, "add", "-A", "--", *files)
        _git(self.upstream, "commit", "-q", "-m", message)
        return _git(self.upstream, "rev-parse", "HEAD")

    def config(self) -> dict[str, Any]:
        return tomllib.loads(_git(self.upstream, "show", f"{self.base}:{DEFAULT_CONFIG_PATH}"))

    def amend_config(self, **changes: Any) -> str:
        """A maintainer's commit on ``main`` that changes top-level sections of the config."""
        text = _git(self.upstream, "show", f"{self.base}:{DEFAULT_CONFIG_PATH}")
        for key, value in changes.items():
            section, name = key.split("__")
            text = _replace_toml_value(text, section, name, value)
        self.base = self.commit(self.base, {DEFAULT_CONFIG_PATH: text}, "Amend", branch="main")
        return self.base

    def approve(self, *digests: str) -> str:
        return self.amend_config(contracts__approved_bindings=list(digests))

    def change(
        self, change: str, extra: Mapping[str, str | None] | None = None, *, bindings: bool = True
    ) -> str:
        """A pull request's head commit: a change set's revision and bindings, plus extras."""
        files: dict[str, str | None] = {**change_files(change, bindings=bindings), **(extra or {})}
        return self.commit(self.base, files, f"Change: {change}")

    def head(self, files: Mapping[str, str | None]) -> str:
        return self.commit(self.base, files, "Pull request")

    def event(self, head_sha: str, *, from_fork: bool = False, base_sha: str | None = None) -> Path:
        self._runs += 1
        payload = {
            "number": 7,
            "pull_request": {
                "number": 7,
                "base": {"ref": "main", "sha": base_sha or self.base},
                "head": {
                    "sha": head_sha,
                    "repo": {
                        "full_name": "forker/subscriptions" if from_fork else "acme/subscriptions",
                        "fork": from_fork,
                    },
                },
            },
            "repository": {"full_name": "acme/subscriptions"},
        }
        path = self.root / f"event-{self._runs}.json"
        path.write_text(json.dumps(payload), "utf-8")
        return path

    def run(
        self,
        head_sha: str,
        *,
        event_name: str = "pull_request",
        from_fork: bool = False,
        config_path: str = DEFAULT_CONFIG_PATH,
        postgres_image: str | None = None,
    ) -> Outcome:
        """Run the Action for a head commit as a job would, with canary secrets in the job."""
        event = self.event(head_sha, from_fork=from_fork)
        report_dir = self.root / f"report-{self._runs}"
        summary = self.root / f"summary-{self._runs}.md"
        output = self.root / f"output-{self._runs}.txt"
        environ = {
            **{k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")},
            **CANARY,
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "1",
        }
        inputs = Inputs(
            workspace=self.workspace,
            config_path=config_path,
            report_dir=report_dir,
            environ=environ,
            **({"postgres_image": postgres_image} if postgres_image else {}),
        )
        exit_code = run(inputs)
        report = json.loads((report_dir / "report.json").read_text("utf-8"))
        outputs = dict(
            line.split("=", 1) for line in output.read_text("utf-8").splitlines() if "=" in line
        )
        return Outcome(exit_code, report, summary.read_text("utf-8"), outputs)


def _replace_toml_value(text: str, section: str, name: str, value: Any) -> str:
    """Replace ``name = ...`` inside ``[section]`` of a simple TOML document."""
    rendered = json.dumps(value) if not isinstance(value, str) else f'"{value}"'
    lines = text.splitlines()
    current = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
        elif current == section and stripped.split("=", 1)[0].strip() == name:
            lines[index] = f"{name} = {rendered}"
            break
    else:
        raise KeyError(f"{section}.{name} is not in the configuration")
    return "\n".join(lines) + "\n"
