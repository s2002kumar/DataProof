"""Commit-bound GitHub check runs (ARCHITECTURE, GitHub connector).

Each pull request run is reported by one check run named ``DataProof`` on its exact head
commit, with the run's ID as its external ID. Its status follows the run:

* queued: ``queued``;
* working: ``in_progress``;
* reported: ``completed`` with the report's conclusion and its coverage table;
* interrupted before it reported: ``completed`` with ``failure`` (an error), ``timed_out``,
  or ``cancelled`` (cancelled or superseded). An interrupted run is never green.

A required check reads only the check run created last with its name on a commit, whatever
order check runs completed in, and an earlier success does not count (probed on GitHub:
docs/DEMO.md, "Check-run finality"). So on each commit the check run created last must be the
newest run's. A run that has not reached GitHub by the time a newer pull request run exists on
its commit is never given a check run: created after the newer run's, it would be the one the
required check reads, and an older run's green could hide a newer run's BLOCKED. That is
recorded once (an omission) and the newer run reports instead. A check run GitHub already shows
is always brought to completion, so none is left open.

The publisher works from the control database. Each status is recorded once GitHub has
accepted it, so a publisher that crashed repeats only what is missing. A lock per commit keeps
two publishers from creating two check runs, or check runs out of their runs' order, and an
existing check run with the run's external ID is found before a new one is created. The summary
is built from trusted results only (check IDs, outcomes and counts); no text from the pull
request appears in it.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from dataproof_api.github_app import GitHubApp, GitHubError
from dataproof_contracts import GitHubCheckConclusion, ReportVerdict, ReviewRun, RunState
from dataproof_control import (
    INTERRUPTED_STATES,
    CheckRun,
    GitHubRecords,
    ReportRecord,
    RunResults,
    RunStore,
    Transition,
)

log = logging.getLogger(__name__)

CHECK_NAME = "DataProof"


@dataclass(frozen=True)
class CheckStatus:
    status: str
    """``queued``, ``in_progress`` or ``completed``."""
    title: str
    summary: str
    conclusion: GitHubCheckConclusion | None = None


def _interruption(transitions: Sequence[Transition]) -> RunState | None:
    states = [t.to_state for t in transitions if t.to_state in INTERRUPTED_STATES]
    return states[-1] if states else None


def check_status(
    run: ReviewRun, report: ReportRecord | None, transitions: Sequence[Transition]
) -> CheckStatus:
    if report is not None:
        return CheckStatus(
            "completed",
            title=_title(report),
            summary=_summary(run, report),
            conclusion=report.conclusion,
        )
    if run.state is RunState.QUEUED:
        return CheckStatus("queued", "Queued", _provenance(run))
    interruption = _interruption(transitions)
    if interruption is None and run.state not in (RunState.COMPLETE, RunState.SUPERSEDED):
        return CheckStatus("in_progress", "Running", _provenance(run))
    if run.superseded_by is not None:
        conclusion, title = GitHubCheckConclusion.CANCELLED, "Superseded by a newer run"
    elif interruption is RunState.TIMED_OUT:
        conclusion, title = GitHubCheckConclusion.TIMED_OUT, "Timed out before it was verified"
    elif interruption is RunState.CANCELLED:
        conclusion, title = GitHubCheckConclusion.CANCELLED, "Cancelled before it was verified"
    else:
        code = run.error_code.value if run.error_code else "UNKNOWN"
        conclusion, title = GitHubCheckConclusion.FAILURE, f"Not verified: ERROR {code}"
    summary = (
        f"**{title}.** This commit was not verified, so this check is not green.\n\n"
        + _provenance(run)
    )
    return CheckStatus("completed", title, summary, conclusion)


def _title(report: ReportRecord) -> str:
    if report.verdict is ReportVerdict.NOT_EXECUTED:
        return f"NOT_EXECUTED: {report.report.get('not_executed_reason', 'policy')}"
    coverage = report.report.get("coverage", [])
    failed = sum(1 for c in coverage if c["required"] and c["outcome"] in ("FAIL", "ERROR"))
    not_run = sum(1 for c in coverage if c["required"] and c["outcome"] == "NOT_RUN")
    detail = []
    if failed:
        detail.append(f"{failed} required check{'s' if failed != 1 else ''} failed or errored")
    if not_run:
        detail.append(f"{not_run} required check{'s' if not_run != 1 else ''} not run")
    return report.verdict.value + (f": {'; '.join(detail)}" if detail else "")


MAX_EXAMPLE_ROWS = 5
"""Mismatch examples shown per failing check; the run holds the bounded full list."""
MAX_CELL_CHARS = 60
MAX_DIAGNOSIS_LINES = 6
MAX_DIAGNOSIS_CHARS = 400
RULES_MODEL_PREFIX = "dataproof:rules"
"""Only the deterministic diagnosis is quoted in the summary. A model's hypotheses are shown in
the dashboard, where they are labelled; the summary carries trusted results alone."""

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


def _plural(count: int, noun: str) -> str:
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {noun}{'es' if noun.endswith(('ch', 'sh', 's', 'x')) else 's'}"


def _cell(text: str, limit: int = MAX_CELL_CHARS) -> str:
    """Untrusted text (a value the candidate database holds) made safe for a Markdown cell."""
    text = " ".join(text.split()).replace("|", "\\|").replace("`", "'")
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
        return _cell(f"'{value.get('value', '')}'")
    return _cell(str(value.get("value", "")))


def _key(key: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(_value(part) for part in key)


def _meaning(
    verdict: ReportVerdict, coverage: Sequence[Mapping[str, Any]], report: Mapping[str, Any]
) -> str:
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
            text += f" {_plural(awaiting, 'check')} wait for a proposed binding to be reviewed."
        return text
    passed = sum(1 for c in required if c["outcome"] == "PASS")
    text = (
        f"Every required check passed ({_plural(passed, 'required check')}, "
        f"{_plural(len(optional), 'optional')}) on this exact commit, fixture, contracts and "
        "engine version. That is what this check establishes, and no more."
    )
    if verdict is ReportVerdict.CHECKED_WITH_WARNINGS:
        text += f" {_plural(warned, 'optional check')} warned."
    return text


def _next_step(
    verdict: ReportVerdict, coverage: Sequence[Mapping[str, Any]], report: Mapping[str, Any]
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
            return (
                "A maintainer reviews each proposed binding in the dashboard (what it reads, and "
                "which relations the pull request did not create or alter) and approves or rejects "
                "it. An approval starts a new run on this commit; this check is not green until "
                "that run passes."
            )
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


def _failures(coverage: Sequence[Mapping[str, Any]]) -> list[str]:
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
        examples: Sequence[Mapping[str, Any]] = check.get("examples") or []
        if examples:
            lines += [
                "",
                "| Key | Column | Expected (reference) | Observed (candidate) |",
                "| --- | --- | --- | --- |",
            ]
            for example in examples[:MAX_EXAMPLE_ROWS]:
                column = example.get("column")
                where = f"`{_cell(column)}`" if column else "presence"
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


def _coverage(coverage: Sequence[Mapping[str, Any]]) -> list[str]:
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

    def tally(checks: Sequence[Mapping[str, Any]]) -> str:
        return ", ".join(
            f"{sum(1 for c in checks if c['outcome'] == outcome)} {label}"
            for outcome, label in (
                ("PASS", "passed"),
                ("FAIL", "failed"),
                ("ERROR", "errored"),
                ("NOT_RUN", "not run"),
            )
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
    investigations: Sequence[Mapping[str, Any]] = report.get("investigations") or []
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
            f"- {_cell(s, MAX_DIAGNOSIS_CHARS)}" for s in statements[:MAX_DIAGNOSIS_LINES] if s
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


def _summary(run: ReviewRun, report: ReportRecord) -> str:
    """The check run's summary: verdict and meaning first, then what to do, the failing checks
    with bounded expected-versus-observed examples, the coverage table, the evidence digests
    and the rule-based diagnosis. Trusted results only: values from the candidate database are
    escaped and bounded, and no pull-request text or model text appears."""
    coverage: list[Mapping[str, Any]] = report.report.get("coverage", [])
    verdict = report.verdict
    lines = [
        f"## {verdict.value} (`{report.conclusion.value}`)",
        "",
        _meaning(verdict, coverage, report.report),
        "",
        f"**Next:** {_next_step(verdict, coverage, report.report)}",
        "",
        *_failures(coverage),
        *_coverage(coverage),
        *_evidence(report.report),
        *_diagnosis(report.report),
        _provenance(run),
    ]
    return "\n".join(lines)


def _provenance(run: ReviewRun) -> str:
    inputs = run.inputs
    return (
        f"Run `{run.run_id}`: base `{inputs.base_sha}`, head `{inputs.head_sha}`, "
        f"configuration `{inputs.config_digest[:12]}`."
    )


class CheckPublisher:
    def __init__(
        self,
        store: RunStore,
        results: RunResults,
        records: GitHubRecords,
        github: GitHubApp,
        installations: Mapping[str, int],
        *,
        details_url: Callable[[UUID], str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.results = results
        self.records = records
        self.github = github
        self.installations = dict(installations)
        """Repository to the GitHub App installation that reports on it."""
        self.details_url = details_url
        self.clock = clock

    def publish_pending(self, limit: int = 50) -> list[UUID]:
        """Bring every unfinished check run up to date; the runs something was sent for."""
        sent = []
        for run_id in self.records.unpublished(list(self.installations), limit):
            try:
                if self.publish(run_id):
                    sent.append(run_id)
            except GitHubError as error:
                log.warning("check run for %s not published: %s", run_id, error)
        return sent

    def publish(self, run_id: UUID) -> bool:
        """Publish the run's current status unless it already was; True if anything was sent."""
        inputs = self.store.get_run(run_id).inputs
        installation = self.installations.get(inputs.repository)
        if installation is None or inputs.pull_request is None:
            return False
        with self.records.publishing(inputs.repository, inputs.head_sha) as held:
            if not held:
                return False
            run = self.store.get_run(run_id)
            status = check_status(run, self.results.report(run_id), self.store.transitions(run_id))
            if status.status in self.records.publications(run_id):
                return False
            payload: dict[str, Any] = {
                "name": CHECK_NAME,
                "status": status.status,
                "output": {"title": status.title, "summary": status.summary},
            }
            if self.details_url is not None:
                payload["details_url"] = self.details_url(run_id)
            if status.conclusion is not None:
                payload["conclusion"] = status.conclusion.value
                payload["completed_at"] = self.clock().isoformat()
            repository = self.github.repository(installation, inputs.repository)
            existing = self.records.check_run(run_id)
            if existing is None:
                check_run_id = repository.find_check_run(inputs.head_sha, CHECK_NAME, str(run_id))
                if check_run_id is None:
                    newer = self.records.newer_run(run_id)
                    if newer is not None:
                        self.records.record_omission(run_id, newer)
                        log.info(
                            "no check run for %s: newer run %s is on its commit", run_id, newer
                        )
                        return False
                    check_run_id = repository.create_check_run(
                        {**payload, "head_sha": inputs.head_sha, "external_id": str(run_id)}
                    )
                else:
                    repository.update_check_run(check_run_id, payload)
                self.records.record_check_run(
                    CheckRun(run_id, inputs.repository, inputs.head_sha, check_run_id)
                )
            else:
                repository.update_check_run(existing.check_run_id, payload)
            self.records.record_publication(
                run_id, status.status, status.conclusion.value if status.conclusion else None
            )
            return True
