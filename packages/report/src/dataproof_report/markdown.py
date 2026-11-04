"""The report as a reviewer reads it on GitHub, from trusted results only.

The input is the report mapping a run records (``pipeline.report``): ``coverage`` rows with
outcomes, reasons, counts and bounded typed mismatch examples; ``evidence`` digests;
``investigations``; and, for a refused run, ``not_executed_reason``. The summary reads top down:

1. the verdict and what it means;
2. what to do next;
3. each failing check, with bounded expected-versus-observed examples;
4. the coverage table with a tally, naming unsupported checks;
5. the evidence digests;
6. the rule-based diagnosis.

Nothing from the pull request's text and nothing a model wrote appears here. A mismatch
example's observed value comes from the candidate database, which the pull request's migration
wrote, so every value is escaped for Markdown and HTML and bounded; the whole summary stays
within GitHub's 65,535-character limit however many checks fail.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from dataproof_contracts import GitHubCheckConclusion, ReportVerdict

MAX_EXAMPLE_ROWS = 5
"""Mismatch examples shown per failing check; the run holds the bounded full list."""
MAX_CELL_CHARS = 60
MAX_DIAGNOSIS_LINES = 6
MAX_DIAGNOSIS_CHARS = 400
RULES_MODEL_PREFIX = "dataproof:rules"
"""Only the deterministic diagnosis is quoted. A model's hypotheses are shown in the dashboard,
where they are labelled; the summary carries trusted results alone."""

_NOT_EXECUTED_NEXT = {
    "FORK_PULL_REQUEST": (
        "Pull requests from forks are not executed. Open the pull request from a branch in this "
        "repository."
    ),
    "DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL": (
        "The change touches a runtime or dependency file, which is not executed until a trusted "
        "image is approved. Split the migration from the dependency change, or have the new "
        "image approved first."
    ),
    "REPOSITORY_NOT_ALLOWLISTED": "This repository is not configured for DataProof.",
}

Rows = Sequence[Mapping[str, Any]]


def _plural(count: int, noun: str) -> str:
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {noun}{'es' if noun.endswith(('ch', 'sh', 's', 'x')) else 's'}"


def escape_cell(text: str, limit: int = MAX_CELL_CHARS) -> str:
    """Untrusted text (a value the candidate database holds) made safe for a Markdown cell.

    Pipes would end the cell, backticks would open or close code, and HTML renders in a job
    summary, so each is neutralized; whitespace is collapsed and the text is bounded.
    """
    text = " ".join(text.split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("|", "\\|").replace("`", "'")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _value(value: Mapping[str, Any] | None) -> str:
    """A typed value's JSON form (``{"type": ..., "value": ...}``) rendered for a reviewer."""
    if value is None:
        return "absent"
    kind = value.get("type")
    if kind == "null":
        return "NULL"
    if kind == "text":
        return escape_cell(f"'{value.get('value', '')}'")
    return escape_cell(str(value.get("value", "")))


def _key(key: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(_value(part) for part in key)


def render_title(verdict: ReportVerdict, report: Mapping[str, Any]) -> str:
    """One line: the verdict, with how many required checks failed, errored or did not run."""
    if verdict is ReportVerdict.NOT_EXECUTED:
        return f"NOT_EXECUTED: {report.get('not_executed_reason', 'policy')}"
    coverage: Rows = report.get("coverage", [])
    failed = sum(1 for c in coverage if c["required"] and c["outcome"] in ("FAIL", "ERROR"))
    not_run = sum(1 for c in coverage if c["required"] and c["outcome"] == "NOT_RUN")
    detail = []
    if failed:
        detail.append(f"{_plural(failed, 'required check')} failed or errored")
    if not_run:
        detail.append(f"{_plural(not_run, 'required check')} not run")
    return verdict.value + (f": {'; '.join(detail)}" if detail else "")


def _meaning(verdict: ReportVerdict, coverage: Rows, report: Mapping[str, Any]) -> str:
    required = [c for c in coverage if c["required"]]
    failed = sum(1 for c in required if c["outcome"] == "FAIL")
    errored = sum(1 for c in required if c["outcome"] == "ERROR")
    not_run = [c for c in required if c["outcome"] == "NOT_RUN"]
    optional = [c for c in coverage if not c["required"]]
    warned = sum(1 for c in optional if c["outcome"] != "PASS")
    if verdict is ReportVerdict.NOT_EXECUTED:
        reason = report.get("not_executed_reason", "policy")
        return f"Policy refused the run (`{reason}`): no migration ran and nothing was verified."
    if verdict is ReportVerdict.BLOCKED:
        parts = []
        if failed:
            parts.append(
                f"{_plural(failed, 'required check')} failed: the data after the migration "
                "does not match the protected contracts"
            )
        if errored:
            parts.append(f"{_plural(errored, 'required check')} could not be evaluated reliably")
        return ". ".join(parts) + "."
    if verdict is ReportVerdict.INCOMPLETE:
        awaiting = sum(1 for c in not_run if c.get("reason") == "AWAITING_BINDING_REVIEW")
        text = (
            f"{_plural(len(not_run), 'required check')} did not run, so nothing is established "
            "about them; a run is never green by omission."
        )
        if awaiting:
            verb = "waits" if awaiting == 1 else "wait"
            text += f" {_plural(awaiting, 'check')} {verb} for a proposed binding to be reviewed."
        return text
    passed = sum(1 for c in required if c["outcome"] == "PASS")
    text = (
        f"Every required check passed ({_plural(passed, 'required check')}, "
        f"{len(optional)} optional) on this exact commit, fixture, contracts and "
        "engine version. That is what this check establishes, and no more."
    )
    if verdict is ReportVerdict.CHECKED_WITH_WARNINGS:
        text += f" {_plural(warned, 'optional check')} warned."
    return text


def _next_step(
    verdict: ReportVerdict, coverage: Rows, report: Mapping[str, Any], *, approval_hint: str
) -> str:
    if verdict is ReportVerdict.NOT_EXECUTED:
        reason = str(report.get("not_executed_reason", ""))
        return _NOT_EXECUTED_NEXT.get(reason, "See the run for the policy that refused it.")
    if verdict is ReportVerdict.BLOCKED:
        binding_errors = any(
            c["outcome"] == "ERROR" and str(c.get("reason") or "").startswith("BINDING_")
            for c in coverage
        )
        fix = "Fix the migration"
        if binding_errors:
            fix += ", or the binding where the reason is a `BINDING_*` error,"
        return (
            f"Open the run for every mismatch and the diagnosis. {fix} and push a new commit; "
            "it is evaluated as a new run. This verdict does not change on this commit."
        )
    if verdict is ReportVerdict.INCOMPLETE:
        reasons = sorted(
            {
                str(c.get("reason") or "")
                for c in coverage
                if c["outcome"] == "NOT_RUN" and c["required"]
            }
        )
        if "AWAITING_BINDING_REVIEW" in reasons:
            return approval_hint
        if "BINDING_REJECTED" in reasons:
            return "A binding was rejected. Propose a corrected binding in a new commit."
        if "PREREQUISITE_FAILED" in reasons:
            return "A check these depend on did not pass; fix that first and push a new commit."
        named = ", ".join(f"`{r}`" for r in reasons if r) or "unspecified"
        return (
            f"The reason is {named}; see the run. Push a new commit or rerun once it is resolved."
        )
    decision = "Merge and deployment stay your decision."
    if verdict is ReportVerdict.CHECKED_WITH_WARNINGS:
        return f"Read the warnings in the coverage table before merging. {decision}"
    return f"Read the coverage table: it lists exactly what was verified. {decision}"


DASHBOARD_APPROVAL_HINT = (
    "A maintainer reviews each proposed binding in the dashboard (what it reads, and which "
    "relations the pull request did not create or alter) and approves or rejects it. An "
    "approval starts a new run on this commit; this check is not green until that run passes."
)
"""The next step for an awaiting-review run of the GitHub App, where approval is interactive."""


def _failures(coverage: Rows) -> list[str]:
    failing = [c for c in coverage if c["outcome"] in ("FAIL", "ERROR")]
    if not failing:
        return []
    lines = ["### Failing checks", ""]
    for check in failing:
        required = "required" if check["required"] else "optional"
        if check["outcome"] == "ERROR":
            code = check.get("reason") or "unknown"
            lines.append(f"**`{check['check_id']}`** ({required}): ERROR `{code}`.")
            lines.append("")
            continue
        count = int(check.get("mismatch_count", 0))
        expected = check.get("expected_key_count")
        universe = f" of {expected:,} expected keys" if isinstance(expected, int) else ""
        lines.append(
            f"**`{check['check_id']}`** ({required}): FAIL, {_plural(count, 'mismatch')}{universe}."
        )
        examples: Rows = check.get("examples") or []
        if examples:
            lines += [
                "",
                "| Key | Column | Expected (reference) | Observed (candidate) |",
                "| --- | --- | --- | --- |",
            ]
            for example in examples[:MAX_EXAMPLE_ROWS]:
                column = example.get("column")
                where = f"`{escape_cell(column)}`" if column else "presence"
                lines.append(
                    f"| {_key(example.get('key', ()))} | {where} "
                    f"| {_value(example.get('expected'))} | {_value(example.get('observed'))} |"
                )
            shown = min(len(examples), MAX_EXAMPLE_ROWS)
            lines.append("")
            lines.append(
                f"_{shown} of {_plural(count, 'mismatch')} shown; the run holds the bounded "
                "full list._"
            )
        lines.append("")
    return lines


def _coverage(coverage: Rows) -> list[str]:
    lines = ["### Coverage", ""]
    if not coverage:
        return [*lines, "No checks ran.", ""]
    lines += [
        "| Check | Required | Outcome | Reason | Keys compared | Mismatches |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for check in coverage:
        keys = check.get("expected_key_count")
        lines.append(
            f"| `{check['check_id']}` | {'yes' if check['required'] else 'no'} "
            f"| {check['outcome']} | {check.get('reason') or ''} "
            f"| {f'{keys:,}' if isinstance(keys, int) else ''} | {check.get('mismatch_count', 0)} |"
        )
    required = [c for c in coverage if c["required"]]
    optional = [c for c in coverage if not c["required"]]

    def tally(checks: Rows) -> str:
        outcomes = (("PASS", "passed"), ("FAIL", "failed"), ("ERROR", "errored"))
        return ", ".join(
            f"{sum(1 for c in checks if c['outcome'] == outcome)} {label}"
            for outcome, label in (*outcomes, ("NOT_RUN", "not run"))
        )

    lines.append("")
    lines.append(f"Required: {tally(required)}. Optional: {tally(optional)}.")
    unsupported = [
        c for c in coverage if c["outcome"] == "NOT_RUN" and c.get("reason") == "UNSUPPORTED"
    ]
    if unsupported:
        lines.append(
            f"{_plural(len(unsupported), 'check')} unsupported for this change and not evaluated; "
            "an unsupported check is never counted as passed."
        )
    return [*lines, ""]


def _evidence(report: Mapping[str, Any]) -> list[str]:
    evidence = report.get("evidence") or {}
    manifest = evidence.get("manifest_sha256")
    if not manifest:
        return []
    parts = [f"evidence manifest `{manifest[:12]}`"]
    for environment, snapshot in sorted((evidence.get("snapshots") or {}).items()):
        digest = str(snapshot.get("sha256", ""))[:12]
        verified = (
            "restore verified" if snapshot.get("restore_verified") else "restore not verified"
        )
        parts.append(f"{environment.lower()} snapshot `{digest}` ({verified})")
    return [
        "### Evidence",
        "",
        "Every result above cites the queries it ran and the digests of what they returned, "
        f"sealed with the run: {'; '.join(parts)}. The snapshots outlive the environments.",
        "",
    ]


def _diagnosis(report: Mapping[str, Any]) -> list[str]:
    investigations: Rows = report.get("investigations") or []
    if not investigations:
        return []
    statements = [
        str(h.get("statement", ""))
        for i in investigations
        if str(i.get("model", "")).startswith(RULES_MODEL_PREFIX)
        for h in (i.get("hypotheses") or [])
    ]
    lines = []
    if statements:
        lines += ["### Rule-based diagnosis (advisory)", ""]
        lines += [
            f"- {escape_cell(s, MAX_DIAGNOSIS_CHARS)}"
            for s in statements[:MAX_DIAGNOSIS_LINES]
            if s
        ]
        if len(statements) > MAX_DIAGNOSIS_LINES:
            lines.append(f"- … and {len(statements) - MAX_DIAGNOSIS_LINES} more in the run.")
        lines.append("")
    lines.append(
        f"{_plural(len(investigations), 'advisory finding')} are attached to the run: risks named "
        "before the migration ran, and explanations of failures. They never change a check or "
        "the verdict."
    )
    return [*lines, ""]


def render_summary(
    verdict: ReportVerdict,
    conclusion: GitHubCheckConclusion,
    report: Mapping[str, Any],
    *,
    provenance: str,
    approval_hint: str = DASHBOARD_APPROVAL_HINT,
) -> str:
    """The whole summary. ``provenance`` is the trusted last line (commits, configuration);
    ``approval_hint`` is the next step when a binding awaits review, which differs between the
    App (interactive approval) and the Action (an approval on the base branch)."""
    coverage: Rows = report.get("coverage", [])
    lines = [
        f"## {verdict.value} (`{conclusion.value}`)",
        "",
        _meaning(verdict, coverage, report),
        "",
        f"**Next:** {_next_step(verdict, coverage, report, approval_hint=approval_hint)}",
        "",
        *_failures(coverage),
        *_coverage(coverage),
        *_evidence(report),
        *_diagnosis(report),
        provenance,
    ]
    return "\n".join(lines)
