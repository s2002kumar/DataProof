"""B3, the AI ablation: the same failures diagnosed with and without a model (#7, #58).

Every database-tier case whose contracts fail or error is diagnosed twice, on the same evidence:

* **model-off**: the rule-based diagnosis every run gets (``dataproof_investigator.diagnose``);
* **model**: the bounded Failure Investigator, as a run's ``INVESTIGATING`` stage runs it. It has
  the five read-only tools over the case's live reference and candidate databases (through a
  read-only role), and the head commit's allowlisted source.

A **live** run calls a paid model. It needs ``--live``, an approved ``--budget-usd`` of at most
``MAX_BUDGET_USD`` and ``ANTHROPIC_API_KEY``. Without them, a scripted model exercises every path
at no cost; that dry run is never reported as a result.

**Accounting.** Every investigation records its input and output tokens, cost, tool calls by
outcome, latency and the model that served it. The model is pinned (no fallbacks), so every turn
has a cost ceiling, and ``CASE_CAP_USD`` is a hard per-case limit: the investigator does not send
a turn that could take it past the cap. A case starts only while the cap still fits in what is
left of the budget, so a live run cannot spend more than its budget.

**Scoring**, frozen here before any live run:

* status: completed (with every citation valid), budget exhausted, invalid output or unavailable;
* localization: how many of the labelled affected event IDs the hypotheses name;
* root cause: whether the hypotheses or suggested action use the family's rubric terms
  (``RUBRIC``). This is a keyword proxy, so every hypothesis is kept verbatim for reading.

Labels are read only after every case has been investigated.
"""

import io
import json
import re
import tarfile
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from commerce_scenario import PUBLISHED_DIGEST
from dataproof_benchmark.evaluate import contracts
from dataproof_benchmark.harness import SAMPLE_APP, DatabaseFactory, candidate, databases, reference
from dataproof_contracts import CheckOutcome, CheckResult, Environment
from dataproof_investigator import (
    PROMPT_VERSION,
    TOOL_VERSION,
    Budget,
    Investigator,
    InvestigatorModel,
    ScriptedModel,
    ToolContext,
    answer,
    call,
    diagnose,
)
from dataproof_runner.project import ProjectConfig
from dataproof_verifier import QueryLimits, verify_contract

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 8000
CASE_CAP_USD = Decimal("0.50")
"""The most one investigation may cost: a hard limit (see the module docstring)."""
MAX_BUDGET_USD = Decimal(10)
"""The largest budget this harness accepts for a live run."""
BUDGET = Budget(max_tool_calls=12, max_turns=16, max_cost_usd=CASE_CAP_USD)
SOURCE_ROOTS: tuple[str, ...] = ProjectConfig.model_fields["investigator_source_roots"].default

RUBRIC: Mapping[str, tuple[tuple[str, ...], ...]] = {
    "join multiplication": (
        ("join", "multipl", "fan-out", "fan out", "order_items", "order item"),
    ),
    "retry effects": (
        ("twice", "again", "retry", "re-run", "rerun", "re-insert", "reinsert", "duplicat"),
    ),
    "balanced loss and duplication": (
        ("missing", "lost", "dropped", "omitted", "skipped"),
        ("duplicat", "twice", "extra", "introduced"),
    ),
    "wrong column mapping": (
        ("placed_at", "wrong column", "mapping", "mapped", "mislabel", "relabel", "wrong kind"),
    ),
    "partial migration": (
        ("missing", "not migrated", "skipped", "boundary", "cutoff", "cut-off", "filtered", "lost"),
    ),
    "lossy numeric conversion": (
        ("round", "truncat", "precision", "scale", "floor", "fractional", "cast"),
    ),
    "timestamp shift": (("time zone", "time-zone", "timezone", "utc", "offset", "local time"),),
    "orphaned reference": (
        (
            "orphan",
            "no matching order",
            "nonexistent",
            "non-existent",
            "does not exist",
            "foreign key",
            "dangling",
            "unknown order",
        ),
    ),
    "transposition": (
        (
            "swap",
            "transpos",
            "exchang",
            "tie",
            "not unique",
            "nondetermin",
            "non-determin",
            "row_number",
        ),
    ),
}
"""Root-cause terms per fault family: every group must match at least one term."""

ModelFactory = Callable[[Mapping[str, CheckResult]], InvestigatorModel]


def source_archive(change_dir: Path) -> bytes:
    """The head commit's source as a run stages it: the sample app plus the change's revision."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(SAMPLE_APP.rglob("*")):
            skipped = {"node_modules", ".next", "__pycache__"} & set(path.parts)
            if path.is_file() and not skipped:
                name = f"sample-app/{path.relative_to(SAMPLE_APP).as_posix()}"
                archive.add(path, arcname=name)
        for revision in sorted(change_dir.glob("*.py")):
            archive.add(revision, arcname=f"sample-app/migrations/versions/{revision.name}")
    return buffer.getvalue()


@contextmanager
def reader(admin_dsn: str) -> Iterator[Callable[[str], str]]:
    """A read-only role, as runs use: a function from a database DSN to the role's DSN."""
    role, password = f"ablation_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        admin.execute(sql.SQL("GRANT pg_read_all_data TO {}").format(sql.Identifier(role)))
        admin.execute(
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                sql.Identifier(role)
            )
        )
    try:
        yield lambda dsn: psycopg.conninfo.make_conninfo(dsn, user=role, password=password)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def checks_and_evidence(
    reference_dsn: str, candidate_dsn: str, change_dir: Path
) -> tuple[dict[str, CheckResult], dict[str, Mapping[str, Any]]]:
    """The contracts' results and evidence records, as ``VERIFYING`` records them."""
    checks: dict[str, CheckResult] = {}
    evidence: dict[str, Mapping[str, Any]] = {}
    with (
        psycopg.connect(reference_dsn, autocommit=True) as ref,
        psycopg.connect(candidate_dsn, autocommit=True) as cand,
    ):
        for contract in contracts(change_dir):
            verification = verify_contract(contract, ref, cand, PUBLISHED_DIGEST, QueryLimits())
            checks[verification.check.check_id] = verification.check
            for item in verification.evidence:
                evidence[item.evidence_id] = {
                    "kind": "query",
                    "environment": item.environment.value,
                    "query_sql": item.query_sql,
                    "sql_sha256": item.sql_sha256,
                    "result_sha256": item.result_sha256,
                    "row_count": item.row_count,
                }
    return checks, evidence


def failed(checks: Mapping[str, CheckResult]) -> list[str]:
    """The checks a run investigates: those that failed or errored, as ``INVESTIGATING`` does."""
    return sorted(
        c for c, r in checks.items() if r.outcome in (CheckOutcome.FAIL, CheckOutcome.ERROR)
    )


def dry_run_model(checks: Mapping[str, CheckResult]) -> InvestigatorModel:
    """A scripted investigator: reads the first failed check, then cites what it read."""
    first = checks[failed(checks)[0]]
    final = {
        "hypotheses": [
            {
                "statement": f"Scripted: {first.check_id} failed.",
                "cited_evidence_ids": ["investigation.tool.1"],
                "confidence": "LOW",
            }
        ],
        "suggested_action": None,
        "caveat": "A dry run: no model was called.",
    }
    return ScriptedModel(
        [call("get_check_result", {"check_id": first.check_id}), answer(json.dumps(final))],
        name="scripted:dry-run",
    )


def investigate(
    admin_dsn: str,
    corpus: Path,
    model_for: ModelFactory,
    budget_usd: Decimal,
    case_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Investigate every failing case; labels are not read here."""
    changes = sorted((corpus / "changes").iterdir())
    if case_ids:
        changes = [c for c in changes if "case-" + c.name.rsplit("-", 1)[1] in case_ids]
    rows: list[dict[str, Any]] = []
    spent = Decimal(0)
    with reader(admin_dsn) as as_reader, databases(admin_dsn) as new_database:
        reference_dsn = reference(new_database)
        for change in changes:
            row = _case(
                new_database, reference_dsn, change, as_reader, model_for, budget_usd - spent
            )
            if row["model"] is not None:
                spent += Decimal(row["model"]["usage"]["cost_usd"])
            rows.append(row)
    return rows


def _case(
    new_database: DatabaseFactory,
    reference_dsn: str,
    change: Path,
    as_reader: Callable[[str], str],
    model_for: ModelFactory,
    remaining: Decimal,
) -> dict[str, Any]:
    case_id = "case-" + change.name.rsplit("-", 1)[1]
    row: dict[str, Any] = {"case_id": case_id, "model_off": None, "model": None, "skipped": None}
    try:
        candidate_dsn = candidate(new_database, change)
    except Exception:  # a failing migration has no data contracts to explain
        row["skipped"] = "the migration failed"
        return row
    checks, evidence = checks_and_evidence(reference_dsn, candidate_dsn, change)
    if not failed(checks):
        row["skipped"] = "no contract failed or errored"
        return row
    run_id = uuid.uuid4()
    row["failed"] = failed(checks)
    row["model_off"] = diagnose(run_id, checks).model_dump(mode="json")
    if remaining < CASE_CAP_USD:
        row["skipped"] = "the budget left cannot cover the per-case cap"
        return row
    tools = ToolContext(
        checks=checks,
        evidence=evidence,
        source_archive=source_archive(change),
        source_roots=SOURCE_ROOTS,
        databases={
            Environment.REFERENCE: as_reader(reference_dsn),
            Environment.CANDIDATE: as_reader(candidate_dsn),
        },
    )
    investigation = Investigator(model_for(checks), BUDGET).investigate(run_id, tools)
    row["model"] = investigation.model_dump(mode="json")
    return row


def _texts(investigation: Mapping[str, Any]) -> list[str]:
    texts = [h["statement"] for h in investigation["hypotheses"]]
    return texts + (
        [investigation["suggested_action"]] if investigation["suggested_action"] else []
    )


def mentioned_numbers(texts: Sequence[str]) -> set[int]:
    return {int(n) for text in texts for n in re.findall(r"(?<![\d.])\d{1,7}(?![\d.])", text)}


def rubric_match(family: str, texts: Sequence[str]) -> bool | None:
    groups = RUBRIC.get(family)
    if groups is None:
        return None
    joined = " ".join(texts).lower()
    return all(any(term in joined for term in group) for group in groups)


def score(investigation: Mapping[str, Any], label: Mapping[str, Any]) -> dict[str, Any]:
    texts = _texts(investigation)
    affected = set(label["affected_events"])
    usage = investigation["usage"]
    outcomes = [c["outcome"] for c in investigation["tool_calls"]]
    return {
        "status": investigation["status"],
        "model": investigation["model"],
        "hypotheses": len(investigation["hypotheses"]),
        "named_affected": len(mentioned_numbers(texts) & affected),
        "affected": len(affected),
        "root_cause": rubric_match(str(label["family"]), texts),
        "tool_calls": {o: outcomes.count(o) for o in sorted(set(outcomes))},
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cost_usd": usage["cost_usd"],
        "latency_ms": usage["latency_ms"],
        "texts": texts,
        "failure": investigation["failure"],
    }


def scored(
    rows: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(label["case_id"]): label for label in labels}
    out = []
    for row in rows:
        label = by_id[row["case_id"]]
        out.append(
            {
                "case_id": row["case_id"],
                "family": label["family"],
                "variant": label["variant"],
                "holdout": label["holdout"],
                "failed": row.get("failed", []),
                "skipped": row["skipped"],
                "model_off": score(row["model_off"], label) if row["model_off"] else None,
                "model": score(row["model"], label) if row["model"] else None,
            }
        )
    return out


def summarize(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ("model_off", "model"):
        arm_rows = [c[arm] for c in cases if c[arm]]
        summary[arm] = {
            "investigations": len(arm_rows),
            "completed": sum(r["status"] == "COMPLETED" for r in arm_rows),
            "root_cause": sum(bool(r["root_cause"]) for r in arm_rows),
            "named_an_affected_event": sum(r["named_affected"] > 0 for r in arm_rows),
            "input_tokens": sum(r["input_tokens"] for r in arm_rows),
            "output_tokens": sum(r["output_tokens"] for r in arm_rows),
            "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in arm_rows), Decimal(0))),
            "tool_calls": sum(sum(r["tool_calls"].values()) for r in arm_rows),
        }
    return summary


def live_budget(budget: Decimal | None, environ: Mapping[str, str]) -> Decimal:
    """The approved budget for a live run, or why there is none."""
    if budget is None:
        raise SystemExit("a live run needs --budget-usd: the budget the owner approved")
    if not Decimal(0) < budget <= MAX_BUDGET_USD:
        raise SystemExit(f"--budget-usd must be more than 0 and at most {MAX_BUDGET_USD}")
    if not environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("a live run needs ANTHROPIC_API_KEY")
    return budget


def live_model() -> InvestigatorModel:
    """The pinned model: no fallbacks, so every turn has a cost ceiling."""
    import anthropic

    from dataproof_investigator.anthropic_model import AnthropicModel

    return AnthropicModel(
        anthropic.Anthropic(), model=MODEL, effort=EFFORT, max_tokens=MAX_TOKENS, fallbacks=False
    )


def results(
    rows: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    *,
    live: bool,
    budget_usd: Decimal,
    model: str,
) -> dict[str, Any]:
    cases = scored(rows, labels)
    return {
        "tier": "B3, the AI ablation, at the database tier",
        "dry_run": not live,
        "model": model,
        "effort": EFFORT,
        "max_tokens": MAX_TOKENS,
        "budget": {
            "approved_usd": str(budget_usd) if live else None,
            "case_cap_usd": str(CASE_CAP_USD),
            "max_tool_calls": BUDGET.max_tool_calls,
            "max_turns": BUDGET.max_turns,
        },
        "prompt_version": PROMPT_VERSION,
        "tool_version": TOOL_VERSION,
        "cases": cases,
        "summary": summarize(cases),
    }


def report(document: Mapping[str, Any]) -> str:
    """The Markdown report of an ablation run."""
    summary = document["summary"]
    lines = [
        "# Benchmark report: the AI ablation (B3)",
        "",
        "Generated by `python -m dataproof_benchmark ablation-report` from `ablation-results.json`."
        " Do not edit by hand.",
        "",
    ]
    if document["dry_run"]:
        lines += ["**A dry run with a scripted model. These are not results.**", ""]
    budget = document["budget"]
    lines += [
        f"- Model: `{document['model']}`, effort {document['effort']}, at most "
        f"{document['max_tokens']} output tokens a turn, no fallbacks.",
        f"- Budget: {budget['approved_usd'] or 'none (dry run)'} USD approved; at most "
        f"{budget['case_cap_usd']} USD, {budget['max_tool_calls']} tool calls and "
        f"{budget['max_turns']} turns an investigation.",
        f"- Prompt `{document['prompt_version']}`, tools `{document['tool_version']}`.",
        "",
        "| | Model-off | Model |",
        "| --- | --- | --- |",
    ]
    rows = (
        ("Investigations", "investigations"),
        ("Completed, every citation valid", "completed"),
        ("Root cause named (rubric)", "root_cause"),
        ("Named an affected event", "named_an_affected_event"),
        ("Tool calls", "tool_calls"),
        ("Input tokens", "input_tokens"),
        ("Output tokens", "output_tokens"),
        ("Cost (USD)", "cost_usd"),
    )
    for title, key in rows:
        lines.append(f"| {title} | {summary['model_off'][key]} | {summary['model'][key]} |")
    lines += ["", "## Cases", ""]
    for case in document["cases"]:
        lines.append(f"### `{case['case_id']}`: {case['family']} ({case['variant']})")
        lines.append("")
        if case["skipped"]:
            lines += [f"Not investigated by the model: {case['skipped']}.", ""]
        for arm, title in (("model_off", "Model-off"), ("model", "Model")):
            scoredarm = case[arm]
            if scoredarm is None:
                continue
            root = {True: "yes", False: "no", None: "n/a"}[scoredarm["root_cause"]]
            lines.append(
                f"- **{title}:** {scoredarm['status']}; root cause {root}; named "
                f"{scoredarm['named_affected']} of {scoredarm['affected']} affected events; "
                f"{sum(scoredarm['tool_calls'].values())} tool calls; "
                f"{scoredarm['input_tokens']} in / {scoredarm['output_tokens']} out tokens; "
                f"{scoredarm['cost_usd']} USD; {scoredarm['latency_ms']} ms."
            )
            lines += [f"  - {text}" for text in scoredarm["texts"]]
        lines.append("")
    return "\n".join(lines)
