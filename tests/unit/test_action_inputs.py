"""The Action's inputs: the repository-owned configuration, the event, and what the job shows."""

import json
from pathlib import Path

import pytest

from dataproof_action import ConfigError, EventError, load_event, parse_config
from dataproof_action.config import binding_approval_digest
from dataproof_action.output import annotations, job_summary, workflow_command
from dataproof_contracts import CandidateBinding
from subscriptions_env import EXAMPLE

CONFIG = (EXAMPLE / "dataproof" / "config.toml").read_text("utf-8")


def test_the_example_configuration_parses_and_digests_stably() -> None:
    config = parse_config(CONFIG)
    assert config.alembic.config == "alembic.ini"
    assert config.contracts.approved_bindings == ()
    assert config.digest() == parse_config(CONFIG).digest()
    assert config.trusted_files("dataproof/config.toml") == (
        "dataproof/config.toml",
        "dataproof/fixture.sql",
    )


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        # An unknown key in any section is refused, so a misspelling cannot relax a setting.
        (
            CONFIG.replace("rejected_bindings = []", "rejected_bindings = []\ntolerance = 1"),
            "tolerance",
        ),
        (
            CONFIG.replace('sql = "dataproof/fixture.sql"', 'sql = "../other/fixture.sql"'),
            "fixture",
        ),
        (CONFIG.replace("version = 1", "version = 2"), "version"),
        (
            CONFIG.replace("approved_bindings = []", f'approved_bindings = ["{"a" * 64}"]').replace(
                "rejected_bindings = []", f'rejected_bindings = ["{"a" * 64}"]'
            ),
            "both approved and rejected",
        ),
        ("version = 1\n[alembic]\nconfig = 'alembic.ini'\n", "fixture"),
        ("not = [toml", "not valid TOML"),
    ],
)
def test_a_configuration_that_could_relax_a_setting_is_refused(text: str, problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        parse_config(text)


def test_an_approval_digest_depends_on_the_binding_text_only() -> None:
    def binding(head_sha: str, path: str, query: str) -> CandidateBinding:
        return CandidateBinding(
            source_path=path,
            head_sha=head_sha,
            query_sql=query,
            column_mapping={"invoice_id": "invoice_id", "total": "total"},
        )

    same = binding("a" * 40, "dataproof/bindings/x.toml", "SELECT invoice_id, total FROM invoices")
    moved = binding("b" * 40, "elsewhere.toml", "SELECT invoice_id, total FROM invoices")
    edited = binding("a" * 40, "dataproof/bindings/x.toml", "SELECT invoice_id, total FROM old")
    assert binding_approval_digest("c", same) == binding_approval_digest("c", moved)
    assert binding_approval_digest("c", same) != binding_approval_digest("c", edited)
    assert binding_approval_digest("c", same) != binding_approval_digest("other", same)
    assert same.digest() != moved.digest(), "the contract digest still binds the commit"


def _event(tmp_path: Path, name: str, **overrides: object) -> dict[str, str]:
    payload = {
        "pull_request": {
            "number": 3,
            "base": {"ref": "main", "sha": "b" * 40},
            "head": {"sha": "c" * 40, "repo": {"full_name": "acme/app", "fork": False}},
        },
        "repository": {"full_name": "acme/app"},
    }
    for key, value in overrides.items():
        section, field = key.split("__")
        payload["pull_request"][section][field] = value  # type: ignore[index]
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), "utf-8")
    return {"GITHUB_EVENT_NAME": name, "GITHUB_EVENT_PATH": str(path)}


def test_pull_request_target_is_refused_before_anything_runs(tmp_path: Path) -> None:
    with pytest.raises(EventError, match="pull_request_target"):
        load_event(_event(tmp_path, "pull_request_target"))
    with pytest.raises(EventError, match="pull_request events only"):
        load_event(_event(tmp_path, "push"))
    with pytest.raises(EventError, match="pull_request events only"):
        load_event({})


def test_a_pull_request_event_is_read_and_forks_are_recognized(tmp_path: Path) -> None:
    event = load_event(_event(tmp_path, "pull_request"))
    assert (event.repository, event.number, event.base_ref) == ("acme/app", 3, "main")
    assert (event.base_sha, event.head_sha) == ("b" * 40, "c" * 40)
    assert not event.from_fork
    assert event.url == "https://github.com/acme/app/pull/3"
    forked = load_event(
        _event(tmp_path, "pull_request", head__repo={"full_name": "x/app", "fork": True})
    )
    assert forked.from_fork
    with pytest.raises(EventError, match="full commit SHA"):
        load_event(_event(tmp_path, "pull_request", head__sha="abc"))
    with pytest.raises(EventError, match="base ref"):
        load_event(_event(tmp_path, "pull_request", base__ref="../evil"))


def test_workflow_commands_are_escaped() -> None:
    line = workflow_command("error", "line1\nline2 100%", file="a,b:c.py", title="T:1")
    assert line == "::error file=a%2Cb%3Ac.py,title=T%3A1::line1%0Aline2 100%25"
    assert workflow_command("notice", "ok") == "::notice::ok"


def _report(verdict: str, coverage: list[dict[str, object]], **extra: object) -> dict[str, object]:
    conclusion = {"CHECKED": "success", "BLOCKED": "failure", "INCOMPLETE": "action_required"}
    return {
        "pull_request": {
            "repository": "acme/app",
            "number": 3,
            "head_sha": "c" * 40,
            "base_ref": "main",
            "base_sha": "d" * 40,
            "base_sha_at_event": "b" * 40,
        },
        "configuration": {
            "path": "dataproof/config.toml",
            "digest": "e" * 64,
            "fixture_sha256": "f" * 64,
            "oracles": {"dataproof/oracles/a.toml": "1" * 64},
            "migration_image": "sha256:" + "2" * 64,
            "postgres_image": "postgres:17-alpine@sha256:" + "3" * 64,
            "backend": "local-docker",
        },
        "verdict": verdict,
        "conclusion": conclusion[verdict],
        "coverage": coverage,
        **extra,
    }


def test_the_job_summary_names_the_base_tip_and_the_approval_path() -> None:
    report = _report(
        "INCOMPLETE",
        [
            {"check_id": "migration.execute", "required": True, "outcome": "PASS"},
            {
                "check_id": "subscriptions.invoice_amounts",
                "required": True,
                "outcome": "NOT_RUN",
                "reason": "AWAITING_BINDING_REVIEW",
            },
        ],
        bindings_awaiting_review=[
            {
                "contract_id": "subscriptions.invoice_amounts",
                "source_path": "dataproof/bindings/invoice_amounts.toml",
                "digest": "9" * 64,
                "relations": [
                    {"name": "public.invoices", "provenance": "ALTERED", "flagged": False},
                    {
                        "name": "public.plans",
                        "provenance": "UNCHANGED",
                        "flagged": True,
                        "through_views": ["public.v"],
                    },
                ],
            }
        ],
        trusted_files_changed=["dataproof/oracles/a.toml"],
    )
    summary = job_summary(report)
    assert summary.startswith("# DataProof\n")
    assert f"verified against `main` at `{'d' * 40}` (the event recorded `{'b' * 40}`" in summary
    assert "## INCOMPLETE (`action_required`)" in summary
    assert "add its digest to `approved_bindings` in `dataproof/config.toml` on `main`" in summary
    assert "a pull request cannot approve its own binding" in summary
    assert f"digest `{'9' * 64}`" in summary
    flagged = "UNCHANGED (flagged: the pull request did not create or alter it)"
    assert f"| `public.plans` | {flagged} | `public.v` |" in summary
    assert "### Trusted files this pull request changed" in summary
    assert "- `dataproof/oracles/a.toml`" in summary
    assert "### Not in this Action" in summary
    assert "Pinned: configuration `eeeeeeeeeeee`" in summary
    lines = annotations(report)
    assert any(
        line.startswith("::error file=dataproof/bindings/invoice_amounts.toml") for line in lines
    )
    assert any(line.startswith("::warning file=dataproof/oracles/a.toml") for line in lines)


def test_annotations_point_failures_at_the_changed_migrations() -> None:
    report = _report(
        "BLOCKED",
        [
            {"check_id": "migration.execute", "required": True, "outcome": "PASS"},
            {"check_id": "c1", "required": True, "outcome": "FAIL", "mismatch_count": 12},
            {"check_id": "c2", "required": True, "outcome": "ERROR", "reason": "QUERY_FAILED"},
            {"check_id": "c3", "required": False, "outcome": "FAIL", "mismatch_count": 1},
        ],
        migration_files_changed=["migrations/versions/0002_x.py"],
    )
    lines = annotations(report)
    assert lines == [
        "::error file=migrations/versions/0002_x.py,line=1,title=DataProof BLOCKED%3A c1::c1 "
        "failed: 12 mismatch(es) between the reference and the migrated candidate. See the job "
        "summary.",
        "::error file=migrations/versions/0002_x.py,line=1,title=DataProof BLOCKED%3A c2::c2 "
        "could not be evaluated: QUERY_FAILED.",
    ]
    checked = _report("CHECKED", [{"check_id": "c1", "required": True, "outcome": "PASS"}])
    assert annotations(checked) == [
        "::notice title=DataProof CHECKED::Every required DataProof check passed on this commit; "
        "see the job summary for exactly what was verified."
    ]
