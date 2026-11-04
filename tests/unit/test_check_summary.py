"""The GitHub check summary: verdict and meaning first, what to do next, bounded
expected-versus-observed examples, the coverage tally, the evidence digests and the rule-based
diagnosis, built from trusted results only."""

from typing import Any

from conftest import ExampleLoader
from dataproof_api.checks import MAX_CELL_CHARS, MAX_EXAMPLE_ROWS, check_status
from dataproof_contracts import GitHubCheckConclusion, ReportVerdict, ReviewRun
from dataproof_control import ReportRecord

GITHUB_SUMMARY_LIMIT = 65_535


def _run(example: ExampleLoader) -> ReviewRun:
    return ReviewRun.model_validate(example("ReviewRun.webhook-complete"))


def _check(
    check_id: str,
    outcome: str,
    *,
    required: bool = True,
    reason: str | None = None,
    mismatches: int = 0,
    examples: list[dict[str, Any]] | None = None,
    keys: int | None = None,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "required": required,
        "outcome": outcome,
        "reason": reason,
        "mismatch_count": mismatches,
        "expected_key_count": keys,
        "observed_key_count": keys,
        "examples": examples or [],
    }


def _mismatch(key: int, column: str | None, expected: Any, observed: Any) -> dict[str, Any]:
    return {
        "key": [{"type": "integer", "value": key}],
        "column": column,
        "expected": expected,
        "observed": observed,
    }


def _summary(example: ExampleLoader, verdict: ReportVerdict, report: dict[str, Any]) -> str:
    conclusion = {
        ReportVerdict.BLOCKED: GitHubCheckConclusion.FAILURE,
        ReportVerdict.INCOMPLETE: GitHubCheckConclusion.ACTION_REQUIRED,
        ReportVerdict.NOT_EXECUTED: GitHubCheckConclusion.ACTION_REQUIRED,
        ReportVerdict.CHECKED: GitHubCheckConclusion.SUCCESS,
        ReportVerdict.CHECKED_WITH_WARNINGS: GitHubCheckConclusion.SUCCESS,
    }[verdict]
    record = ReportRecord(verdict=verdict, conclusion=conclusion, report=report)
    status = check_status(_run(example), record, [])
    assert status.status == "completed"
    return status.summary


def test_candidate_values_are_escaped_and_bounded(example: ExampleLoader) -> None:
    hostile = "x | y `code` \n<img src=x> " + "A" * 200
    report = {
        "coverage": [
            _check(
                "c.text",
                "FAIL",
                mismatches=1,
                keys=3,
                examples=[
                    _mismatch(
                        7,
                        "memo",
                        {"type": "text", "value": "fine"},
                        {"type": "text", "value": hostile},
                    )
                ],
            )
        ]
    }
    summary = _summary(example, ReportVerdict.BLOCKED, report)
    assert summary.startswith("## BLOCKED (`failure`)")
    row = next(line for line in summary.splitlines() if line.startswith("| 7 |"))
    assert "`memo`" in row
    assert "'fine'" in row
    assert "x \\| y 'code' <img src=x>" in row, "pipes escaped, backticks neutralized"
    assert "\n" not in row
    assert "AAAA…" in row
    cells = [c.strip() for c in row.strip("|").split(" | ")]
    assert all(len(c) <= MAX_CELL_CHARS + 2 for c in cells[2:]), cells


def test_blocked_names_the_failure_and_what_to_do(example: ExampleLoader) -> None:
    report = {
        "coverage": [
            _check("migration.execute", "PASS"),
            _check(
                "c1",
                "FAIL",
                mismatches=40,
                keys=1207,
                examples=[
                    _mismatch(
                        n,
                        "amount",
                        {"type": "decimal", "value": "49.99"},
                        {"type": "decimal", "value": "99.98"},
                    )
                    for n in range(8)
                ],
            ),
            _check("c2", "ERROR", reason="BINDING_COVERAGE_GAP"),
            _check("c6", "NOT_RUN", required=False, reason="PREREQUISITE_FAILED"),
        ]
    }
    summary = _summary(example, ReportVerdict.BLOCKED, report)
    assert "1 required check failed" in summary
    assert "1 required check could not be evaluated reliably" in summary
    assert "or the binding where the reason is a `BINDING_*` error" in summary
    assert "**`c1`** (required): FAIL, 40 mismatches of 1,207 expected keys." in summary
    assert "**`c2`** (required): ERROR `BINDING_COVERAGE_GAP`." in summary
    rows = [line for line in summary.splitlines() if line.startswith("| ") and "| 99.98 |" in line]
    assert len(rows) == MAX_EXAMPLE_ROWS
    assert f"_{MAX_EXAMPLE_ROWS} of 40 mismatches shown" in summary
    assert "Required: 1 passed, 1 failed, 1 errored, 0 not run. Optional: 0 passed" in summary


def test_awaiting_binding_review_tells_the_maintainer_what_to_do(example: ExampleLoader) -> None:
    report = {
        "coverage": [
            _check("migration.execute", "PASS"),
            _check("c1", "NOT_RUN", reason="AWAITING_BINDING_REVIEW"),
            _check("c2", "NOT_RUN", reason="AWAITING_BINDING_REVIEW"),
        ]
    }
    summary = _summary(example, ReportVerdict.INCOMPLETE, report)
    assert summary.startswith("## INCOMPLETE (`action_required`)")
    assert "2 required checks did not run" in summary
    assert "never green by omission" in summary
    assert "2 checks wait for a proposed binding to be reviewed" in summary
    assert "**Next:** A maintainer reviews each proposed binding" in summary
    assert "### Failing checks" not in summary


def test_a_missing_presence_is_shown_as_such(example: ExampleLoader) -> None:
    report = {
        "coverage": [
            _check(
                "c1",
                "FAIL",
                mismatches=1,
                keys=10,
                examples=[_mismatch(1005, None, {"type": "integer", "value": 1005}, None)],
            )
        ]
    }
    summary = _summary(example, ReportVerdict.BLOCKED, report)
    assert "| 1005 | presence | 1005 | absent |" in summary


def test_not_executed_explains_the_policy(example: ExampleLoader) -> None:
    report = {"not_executed_reason": "FORK_PULL_REQUEST", "coverage": []}
    summary = _summary(example, ReportVerdict.NOT_EXECUTED, report)
    assert "Policy refused the run (`FORK_PULL_REQUEST`)" in summary
    assert "**Next:** Pull requests from forks are not executed." in summary
    assert "No checks ran." in summary


def test_checked_states_exactly_what_was_verified(example: ExampleLoader) -> None:
    report = {
        "coverage": [
            _check("migration.execute", "PASS"),
            _check("c1", "PASS", keys=1207),
            _check("c6", "PASS", required=False, keys=400),
        ],
        "evidence": {
            "manifest_sha256": "f" * 64,
            "snapshots": {"REFERENCE": {"sha256": "1" * 64, "restore_verified": False}},
        },
    }
    summary = _summary(example, ReportVerdict.CHECKED, report)
    assert summary.startswith("## CHECKED (`success`)")
    assert "Every required check passed (2 required checks, 1 optional)" in summary
    assert "That is what this check establishes, and no more." in summary
    assert "**Next:** Read the coverage table" in summary
    assert "### Failing checks" not in summary
    assert "| `c1` | yes | PASS |  | 1,207 | 0 |" in summary
    assert "reference snapshot `111111111111` (restore not verified)" in summary


def test_unsupported_checks_are_named_and_never_counted_as_passed(
    example: ExampleLoader,
) -> None:
    report = {
        "coverage": [
            _check("c1", "PASS", keys=5),
            _check("c7", "NOT_RUN", required=False, reason="UNSUPPORTED"),
        ]
    }
    summary = _summary(example, ReportVerdict.CHECKED_WITH_WARNINGS, report)
    assert "1 optional check warned." in summary
    assert "1 check unsupported for this change and not evaluated" in summary
    assert "an unsupported check is never counted as passed" in summary
    assert "**Next:** Read the warnings" in summary


def test_only_the_rule_based_diagnosis_is_quoted(example: ExampleLoader) -> None:
    report = {
        "coverage": [_check("c1", "FAIL", mismatches=1, keys=1)],
        "investigations": [
            {
                "model": "dataproof:rules-v1",
                "status": "COMPLETED",
                "hypotheses": [{"statement": "c1: Every changed value is an exact multiple (2x)."}],
            },
            {
                "model": "anthropic:claude-opus-5-5",
                "status": "COMPLETED",
                "hypotheses": [{"statement": "MODEL TEXT that must not be quoted | `x`"}],
            },
        ],
    }
    summary = _summary(example, ReportVerdict.BLOCKED, report)
    assert "### Rule-based diagnosis (advisory)" in summary
    assert "- c1: Every changed value is an exact multiple (2x)." in summary
    assert "MODEL TEXT" not in summary
    assert "2 advisory findings are attached to the run" in summary
    assert "never change a check or the verdict" in summary


def test_a_large_report_stays_within_githubs_summary_limit(example: ExampleLoader) -> None:
    examples = [
        _mismatch(
            n,
            "memo",
            {"type": "text", "value": "m" * 500},
            {"type": "text", "value": "n" * 500},
        )
        for n in range(50)
    ]
    report = {
        "coverage": [
            _check(f"contract.{i:03d}", "FAIL", mismatches=10_000, keys=10_000, examples=examples)
            for i in range(60)
        ],
        "investigations": [
            {
                "model": "dataproof:rules-v1",
                "status": "COMPLETED",
                "hypotheses": [{"statement": "s" * 1000} for _ in range(40)],
            }
        ],
    }
    summary = _summary(example, ReportVerdict.BLOCKED, report)
    assert len(summary) < GITHUB_SUMMARY_LIMIT
    assert "- … and 34 more in the run." in summary
