"""What the job shows: the step summary, annotations and outputs, from trusted results only.

The summary is the same rendering the GitHub App's check runs use (``dataproof_report``), with
what the Action adds: the pull request and the base branch tip it was verified against, any
binding awaiting review and how a maintainer approves it, the trusted files the pull request
changed (ignored until merged), what stays with the full App, and the digests of everything the
run was pinned to. Annotations are workflow commands, so their text is escaped as GitHub
requires; file paths in them come from ``git diff``, never from the pull request's text.

**Untrusted text.** A pull request controls more than its migration: the names of the files it
adds, the content of its bindings (and so the text of any error about them), and the names of
the relations its migration creates. Each of those is escaped wherever it is shown: in the
summary through ``escape_cell`` (Markdown and HTML neutralized, bounded) or ``code``; in a log
line or a workflow command's message through ``one_line``, which collapses it onto one line and
breaks ``::``, so that no text from the pull request can start a line the runner would read as
a workflow command (a forged notice, a fake annotation, ``::stop-commands::``).
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dataproof_contracts import GitHubCheckConclusion, ReportVerdict
from dataproof_report import escape_cell, render_summary

NOT_IN_THE_ACTION = (
    "API behaviour checks (C6) against live previews, previews of both commits, the AI Change "
    "Analyst and Failure Investigator, reproduction on a subset, and interactive binding "
    "approval are capabilities of the DataProof GitHub App, not of this Action."
)


MAX_LINE_CHARS = 500


def one_line(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Untrusted text made safe for a log line or a workflow command's message.

    Whitespace (newlines included) collapses to single spaces, so the text cannot begin a line
    of its own, and ``::`` is broken up, so that it cannot read as a command even where a
    runner would accept one mid-line. The text is bounded.
    """
    text = " ".join(text.split()).replace("::", ": :")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def code(text: str, limit: int = 120) -> str:
    """Untrusted text as an inline code span: backticks cannot close it early."""
    return f"`{escape_cell(text, limit)}`"


def approval_hint(config_path: str, base_ref: str) -> str:
    return (
        "A maintainer reviews each proposed binding below: what it reads, and which relations "
        "the pull request did not create or alter. To approve one, add its digest to "
        f"`approved_bindings` in `{config_path}` on `{base_ref}` through a reviewed change, then "
        "re-run this check. An approval lives on the base branch, so a pull request cannot "
        "approve its own binding, and this check is not green until the approved run passes."
    )


def _bindings_section(pending: Sequence[Mapping[str, Any]]) -> list[str]:
    if not pending:
        return []
    lines = ["### Proposed bindings awaiting review", ""]
    for binding in pending:
        lines.append(
            f"**`{binding['contract_id']}`**, proposed in {code(binding['source_path'])}, digest "
            f"`{binding['digest']}`."
        )
        problem = binding.get("problem")
        if problem:
            lines.append("")
            lines.append(f"Not acceptable as proposed: {escape_cell(str(problem), 300)}")
        relations: Sequence[Mapping[str, Any]] = binding.get("relations") or []
        if relations:
            lines += ["", "| Reads | Provenance | Through views |", "| --- | --- | --- |"]
            for relation in relations:
                flag = (
                    " (flagged: the pull request did not create or alter it)"
                    if relation.get("flagged")
                    else ""
                )
                # Relation names come from the candidate catalog: the migration chose them.
                views = ", ".join(code(v) for v in relation.get("through_views") or []) or ""
                lines.append(
                    f"| {code(relation['name'])} | {relation['provenance']}{flag} | {views} |"
                )
        lines.append("")
    return lines


def _trusted_changes_section(
    paths: Sequence[str], base_ref: str, *, since_merge_base: bool = True
) -> list[str]:
    if not paths:
        return []
    lines = ["### Trusted files this pull request changed", ""]
    lines.append(
        f"DataProof read these from `{base_ref}`, not from the pull request. The change takes "
        "effect after it is merged, so it did not affect this result."
    )
    if not since_merge_base:
        lines.append(
            f"The pull request's merge base with `{base_ref}` was not found in the history "
            f"fetched, so these are the files that differ from `{base_ref}`'s tip: they may "
            f"include changes made on `{base_ref}` after the pull request branched."
        )
    lines.append("")
    lines += [f"- {code(path, 200)}" for path in paths]
    return [*lines, ""]


def job_summary(report: Mapping[str, Any]) -> str:
    """The whole step summary for one run of the Action."""
    pull_request = report["pull_request"]
    configuration = report["configuration"]
    verdict = ReportVerdict(report["verdict"])
    conclusion = GitHubCheckConclusion(report["conclusion"])
    oracles = ", ".join(f"`{p}` {d[:12]}" for p, d in sorted(configuration["oracles"].items()))
    provenance = (
        f"Pinned: configuration `{configuration['digest'][:12]}`, fixture "
        f"`{configuration['fixture_sha256'][:12]}`, oracles {oracles or 'none'}; migration image "
        f"`{configuration['migration_image'][:19]}`, PostgreSQL "
        f"`{configuration['postgres_image']}`, backend `{configuration['backend']}`."
    )
    heading = [
        "# DataProof",
        "",
        f"Pull request #{pull_request['number']} in `{pull_request['repository']}`: head "
        f"`{pull_request['head_sha']}` verified against `{pull_request['base_ref']}` at "
        f"`{pull_request['base_sha']}`"
        + (
            f" (the event recorded `{pull_request['base_sha_at_event']}`; the base branch moved)"
            if pull_request["base_sha_at_event"] != pull_request["base_sha"]
            else ""
        )
        + ".",
        "",
    ]
    body = render_summary(
        verdict,
        conclusion,
        report,
        provenance="",
        approval_hint=approval_hint(configuration["path"], pull_request["base_ref"]),
    ).rstrip("\n")
    sections = [
        *_bindings_section(report.get("bindings_awaiting_review") or []),
        *_trusted_changes_section(
            report.get("trusted_files_changed") or [],
            pull_request["base_ref"],
            since_merge_base=report.get("changed_since") is not None,
        ),
        "### Not in this Action",
        "",
        NOT_IN_THE_ACTION,
        "",
        provenance,
        "",
    ]
    return "\n".join([*heading, body, "", *sections])


def _command_value(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _command_property(text: str) -> str:
    return _command_value(text).replace(":", "%3A").replace(",", "%2C")


def workflow_command(kind: str, message: str, **properties: str) -> str:
    """A ``::kind prop=value::message`` line, escaped as GitHub's workflow commands require."""
    rendered = ",".join(f"{k}={_command_property(v)}" for k, v in properties.items() if v)
    return f"::{kind}{' ' + rendered if rendered else ''}::{_command_value(message)}"


def annotations(report: Mapping[str, Any]) -> list[str]:
    """Annotations for the job: one per failing check on each migration file the pull request
    changed, one for a binding awaiting review, one per trusted file the pull request changed,
    and one notice for a passing run."""
    verdict = ReportVerdict(report["verdict"])
    migration_files: Sequence[str] = report.get("migration_files_changed") or []
    lines = []
    for check in report.get("coverage") or []:
        if check["outcome"] not in ("FAIL", "ERROR") or not check["required"]:
            continue
        if check["outcome"] == "FAIL":
            message = (
                f"{check['check_id']} failed: {check.get('mismatch_count', 0)} mismatch(es) "
                "between the reference and the migrated candidate. See the job summary."
            )
        else:
            message = f"{check['check_id']} could not be evaluated: {check.get('reason')}."
        title = f"DataProof {verdict.value}: {check['check_id']}"
        if migration_files:
            lines += [
                workflow_command("error", message, file=path, line="1", title=title)
                for path in migration_files
            ]
        else:
            lines.append(workflow_command("error", message, title=title))
    for binding in report.get("bindings_awaiting_review") or []:
        lines.append(
            workflow_command(
                "error",
                f"Binding {binding['digest']} for {binding['contract_id']} awaits a maintainer's "
                "approval on the base branch; the checks that depend on it did not run.",
                file=binding["source_path"],
                line="1",
                title="DataProof INCOMPLETE: binding awaiting review",
            )
        )
    for path in report.get("trusted_files_changed") or []:
        lines.append(
            workflow_command(
                "warning",
                "DataProof reads this file from the base branch; this change takes effect after "
                "it is merged and did not affect this result.",
                file=path,
                line="1",
                title="DataProof: trusted file changed",
            )
        )
    if verdict is ReportVerdict.NOT_EXECUTED:
        lines.append(
            workflow_command(
                "error",
                f"DataProof did not run: {report.get('not_executed_reason')}.",
                title="DataProof NOT_EXECUTED",
            )
        )
    if verdict in (ReportVerdict.CHECKED, ReportVerdict.CHECKED_WITH_WARNINGS):
        lines.append(
            workflow_command(
                "notice",
                "Every required DataProof check passed on this commit; see the job summary for "
                "exactly what was verified.",
                title=f"DataProof {verdict.value}",
            )
        )
    return lines


def write_outputs(path: Path | None, values: Mapping[str, str]) -> None:
    """Append ``name=value`` lines to ``$GITHUB_OUTPUT`` (single-line values only)."""
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        for name, value in values.items():
            handle.write(f"{name}={value.replace(chr(10), ' ')}\n")


def write_summary(path: Path | None, text: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
