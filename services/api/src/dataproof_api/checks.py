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


def _summary(run: ReviewRun, report: ReportRecord) -> str:
    coverage: list[Mapping[str, Any]] = report.report.get("coverage", [])
    lines = [f"**{report.verdict.value}** (`{report.conclusion.value}`).", ""]
    if coverage:
        lines += [
            "| Check | Required | Outcome | Reason | Mismatches |",
            "| --- | --- | --- | --- | --- |",
        ]
        for check in coverage:
            lines.append(
                f"| `{check['check_id']}` | {'yes' if check['required'] else 'no'} "
                f"| {check['outcome']} | {check.get('reason') or ''} "
                f"| {check.get('mismatch_count', 0)} |"
            )
    else:
        lines.append("No checks ran.")
    investigations = report.report.get("investigations") or []
    if investigations:
        lines += [
            "",
            f"{len(investigations)} advisory investigation(s) are attached to the run. "
            "They explain failures and never change a check or the verdict.",
        ]
    return "\n".join([*lines, "", _provenance(run)])


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
