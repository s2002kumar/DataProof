"""The Change Analyst against the static risk rules (#7, #58): risks named before anything runs.

Every change set of the corpus (the 31 database-tier cases and the 3 API behaviour cases) is
analyzed as its pull request's head commit is in ``ANALYZING``. The head carries the migration,
the ledger API and frontend, and the bindings, which neither arm may read. Two arms:

* **rules:** ``risk_rules``, deterministic. Its results are committed, and a test regenerates them.
* **model:** ``change_analyst``, the bounded loop with ``read_source`` only. A live run needs an
  approved budget (``ablation.live_budget``) and stops each case at ``CASE_CAP_USD``. Without a
  budget a scripted model runs every path, and that dry run is never reported as a result.

**Scoring**, frozen here, against labels read only after every case has been analyzed:

* a fault is *anticipated* when a flagged risk points at its family. For the rules, that is a
  matched rule whose ``RULE_FAMILIES`` include the family. For the model, it is the family's
  rubric (``ablation.RUBRIC``, and ``API_RUBRIC`` for API behaviour) in its hypotheses;
* an edge case is *relevant* when the suggested edge cases match the family: from a rule that
  anticipates it, or the rubric on the model's suggested edge cases alone;
* a clean control is *alarmed* when any risk is flagged: any rule match, or a model hypothesis of
  MEDIUM or HIGH confidence (LOW means "looks safe, but check").

The rules were written by the corpus's author with the corpus in view. They are generic
patterns with no case-specific literal, but their recall here is an optimistic bound.
"""

import functools
import io
import tarfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from dataproof_benchmark.ablation import RUBRIC, Recorder, measures
from dataproof_benchmark.corpus import LEDGER_API, Case
from dataproof_benchmark.files import BINDINGS
from dataproof_benchmark.harness import SAMPLE_APP
from dataproof_benchmark.pipeline_tier import LEDGER_WEB
from dataproof_contracts import Confidence
from dataproof_investigator import (
    Budget,
    InvestigatorModel,
    ScriptedModel,
    ToolContext,
    answer,
    call,
    change_analyst,
    risk_rules,
)
from dataproof_runner.project import ProjectConfig

CASE_CAP_USD = Decimal("0.15")
"""The most one analysis may cost: a hard limit with a pinned model, as in the ablation."""
BUDGET = Budget(max_tool_calls=8, max_turns=10, max_cost_usd=CASE_CAP_USD)
SOURCE_ROOTS: tuple[str, ...] = ProjectConfig.model_fields["investigator_source_roots"].default
MIGRATIONS = ("sample-app/migrations/",)

RULE_FAMILIES: Mapping[str, frozenset[str]] = {
    "join-in-backfill": frozenset({"join multiplication", "wrong column mapping"}),
    "aggregate-in-backfill": frozenset({"join multiplication"}),
    "rounding": frozenset({"lossy numeric conversion", "API behaviour"}),
    "time-zone-conversion": frozenset({"timestamp shift"}),
    "filtered-backfill": frozenset({"partial migration"}),
    "repeated-insert": frozenset(
        {"retry effects", "balanced loss and duplication", "partial migration"}
    ),
    "row-level-patch": frozenset(
        {
            "transposition",
            "wrong column mapping",
            "orphaned reference",
            "balanced loss and duplication",
            "retry effects",
        }
    ),
    "unconstrained-key": frozenset(
        {"orphaned reference", "balanced loss and duplication", "retry effects"}
    ),
    "deduplication": frozenset({"balanced loss and duplication"}),
    "null-coercion": frozenset({"NULL and empty confusion"}),
    "destructive-ddl": frozenset(),
}
"""The fault families each rule's risk statement anticipates, for scoring only."""

API_RUBRIC: tuple[tuple[str, ...], ...] = (
    ("api", "endpoint", "payments.py", "response", "route"),
    ("refund", "entry_kind", "filter", "round", "quantize", "precision", "decimal place"),
)
RUBRICS: Mapping[str, tuple[tuple[str, ...], ...]] = {**RUBRIC, "API behaviour": API_RUBRIC}

ModelFactory = Callable[[str], InvestigatorModel]
"""A model for one case, given the path of the case's migration (only the dry run uses it)."""


def head_files(case: Case) -> dict[str, str]:
    """What the case's pull request changes, as the pipeline tier commits it."""
    return {
        f"sample-app/migrations/versions/{case.revision}_ledger_backfill.py": case.source(),
        "sample-app/app/payments.py": case.api_source or LEDGER_API.read_text("utf-8"),
        "sample-app/web/lib/payments.ts": LEDGER_WEB.read_text("utf-8"),
        **{
            f"dataproof/bindings/{b.name}": b.read_text("utf-8")
            for b in sorted(BINDINGS.glob("*.toml"))
        },
    }


@functools.cache
def _base_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(SAMPLE_APP.rglob("*")):
        skipped = {"node_modules", ".next", "__pycache__"} & set(path.parts)
        if path.is_file() and not skipped:
            files[f"sample-app/{path.relative_to(SAMPLE_APP).as_posix()}"] = path.read_bytes()
    return files


def head_archive(changed: Mapping[str, str]) -> bytes:
    """The head commit's source: the sample app at its base, with the changed files on top."""
    files = _base_files() | {name: text.encode() for name, text in changed.items()}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def dry_run_model(revision: str) -> InvestigatorModel:
    """A scripted analyst: reads the start of the migration, then cites what it read."""
    final = (
        '{"hypotheses": [{"statement": "Scripted: the migration was read.", '
        '"cited_evidence_ids": ["investigation.tool.1"], "confidence": "LOW"}], '
        '"suggested_action": null, "caveat": "A dry run: no model was called."}'
    )
    return ScriptedModel(
        [call("read_source", {"path": revision, "start_line": 1, "end_line": 40}), answer(final)],
        name="scripted:dry-run",
    )


def analyze(
    cases: Sequence[Case],
    model_for: ModelFactory | None,
    budget_usd: Decimal,
) -> list[dict[str, Any]]:
    """Analyze every case with the rules and, if given, the model; labels are not read here."""
    rows: list[dict[str, Any]] = []
    spent = Decimal(0)
    for case in cases:
        changed = head_files(case)
        readable = {p: t for p, t in changed.items() if p.startswith(SOURCE_ROOTS)}
        run_id = uuid.uuid4()
        row: dict[str, Any] = {"case_id": case.case_id, "rules": None, "model": None}
        row["rules"] = risk_rules(run_id, readable, MIGRATIONS).model_dump(mode="json")
        if model_for is not None:
            if budget_usd - spent < CASE_CAP_USD:
                row["skipped"] = "the budget left cannot cover the per-case cap"
            else:
                row |= _model_arm(run_id, case, changed, model_for)
                spent += Decimal(row["model"]["usage"]["cost_usd"])
        rows.append(row)
    return rows


def _model_arm(
    run_id: uuid.UUID, case: Case, changed: Mapping[str, str], model_for: ModelFactory
) -> dict[str, Any]:
    revision = f"sample-app/migrations/versions/{case.revision}_ledger_backfill.py"
    recorder = Recorder(model_for(revision))
    context = ToolContext(
        checks={},
        evidence={},
        source_archive=head_archive(changed),
        source_roots=SOURCE_ROOTS,
        databases={},
        changed_paths=tuple(sorted(changed)),
    )
    analysis = change_analyst(recorder, BUDGET).investigate(run_id, context)
    measured = measures(analysis, recorder.tool_evidence(), recorder.final_text() or "")
    return {
        "model": analysis.model_dump(mode="json"),
        "model_measures": measured,
        "transcript": recorder.turns,
    }


def _texts(analysis: Mapping[str, Any]) -> list[str]:
    return [h["statement"] for h in analysis["hypotheses"]]


def score_rules(analysis: Mapping[str, Any], family: str, kind: str) -> dict[str, Any]:
    matched = [h["statement"].split(" ", 1)[0] for h in analysis["hypotheses"]]
    pointing = [rule for rule in matched if family in RULE_FAMILIES.get(rule, frozenset())]
    return {
        "risks": len(matched),
        "matched": matched,
        "anticipated": bool(pointing) if kind == "fault" else None,
        "relevant_edge_case": bool(pointing) if kind == "fault" else None,
        "alarmed": bool(matched) if kind == "clean" else None,
    }


def score_model(
    analysis: Mapping[str, Any], measured: Mapping[str, Any], family: str, kind: str
) -> dict[str, Any]:
    texts = _texts(analysis)
    rubric = RUBRICS.get(family)
    raised = [
        h
        for h in analysis["hypotheses"]
        if h["confidence"] in (Confidence.MEDIUM.value, Confidence.HIGH.value)
    ]
    usage = analysis["usage"]
    return {
        "status": analysis["status"],
        "risks": len(raised),
        "anticipated": _rubric(rubric, texts) if kind == "fault" else None,
        "relevant_edge_case": _rubric(rubric, [analysis["suggested_action"] or ""])
        if kind == "fault"
        else None,
        "alarmed": bool(raised) if kind == "clean" else None,
        "citations": measured["citations"],
        "valid_citations": measured["valid_citations"],
        "unsupported_specifics": measured.get("unsupported_specifics", []),
        "cost_usd": usage["cost_usd"],
        "latency_ms": usage["latency_ms"],
        "texts": texts + ([analysis["suggested_action"]] if analysis["suggested_action"] else []),
        "failure": analysis["failure"],
    }


def _rubric(groups: tuple[tuple[str, ...], ...] | None, texts: Sequence[str]) -> bool | None:
    if groups is None:
        return None
    joined = " ".join(texts).lower()
    return all(any(term in joined for term in group) for group in groups)


def scored(
    rows: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(label["case_id"]): label for label in labels}
    out = []
    for row in rows:
        label = by_id[row["case_id"]]
        family, kind = str(label["family"]), str(label["kind"])
        out.append(
            {
                "case_id": row["case_id"],
                "family": family,
                "kind": kind,
                "variant": label["variant"],
                "holdout": label["holdout"],
                "detected_by_contracts": bool(label["failing"]),
                "rules": score_rules(row["rules"], family, kind),
                "model": score_model(row["model"], row["model_measures"], family, kind)
                if row.get("model")
                else None,
                "skipped": row.get("skipped"),
            }
        )
    return out


def summarize(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    faults = [c for c in cases if c["kind"] == "fault"]
    clean = [c for c in cases if c["kind"] == "clean"]
    summary: dict[str, Any] = {"faults": len(faults), "clean_controls": len(clean)}
    for arm in ("rules", "model"):
        arm_faults = [c[arm] for c in faults if c[arm]]
        arm_clean = [c[arm] for c in clean if c[arm]]
        summary[arm] = {
            "analyzed": len(arm_faults) + len(arm_clean),
            "faults_anticipated": sum(bool(r["anticipated"]) for r in arm_faults),
            "relevant_edge_cases": sum(bool(r["relevant_edge_case"]) for r in arm_faults),
            "clean_controls_alarmed": sum(bool(r["alarmed"]) for r in arm_clean),
            "risks_flagged": sum(r["risks"] for r in arm_faults + arm_clean),
        }
        if arm == "model":
            rows = arm_faults + arm_clean
            summary[arm] |= {
                "citations": sum(r["citations"] for r in rows),
                "valid_citations": sum(r["valid_citations"] for r in rows),
                "unsupported_specifics": sum(len(r["unsupported_specifics"]) for r in rows),
                "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))),
            }
    return summary


def results(
    rows: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    *,
    model: str | None,
    live: bool,
    budget_usd: Decimal | None,
) -> dict[str, Any]:
    cases = scored(rows, labels)
    return {
        "tier": "change analysis, before anything runs (ANALYZING)",
        "model": model,
        "dry_run": model is not None and not live,
        "budget": {
            "approved_usd": str(budget_usd) if live else None,
            "case_cap_usd": str(CASE_CAP_USD),
            "max_tool_calls": BUDGET.max_tool_calls,
            "max_turns": BUDGET.max_turns,
        },
        "rule_families": {rule: sorted(f) for rule, f in RULE_FAMILIES.items()},
        "cases": cases,
        "summary": summarize(cases),
    }


def report(document: Mapping[str, Any]) -> str:
    """The Markdown report of a change-analysis run."""
    summary = document["summary"]
    model_ran = summary["model"]["analyzed"] > 0
    lines = [
        "# Benchmark report: change analysis before anything runs",
        "",
        "Generated by `python -m dataproof_benchmark change-analysis-report` from "
        "`change-analysis-results.json`. Do not edit by hand.",
        "",
        "Each pull request of the corpus, analyzed as it is in `ANALYZING`, before any "
        "environment exists. Neither arm decides anything: the data contracts do, after the "
        "migration has run. See [the method](../README.md#change-analysis).",
        "",
    ]
    if document["dry_run"]:
        lines += ["**The model arm is a dry run with a scripted model. It is not a result.**", ""]
    elif not model_ran:
        lines += [
            "**The model arm has not run:** it waits for an approved model budget. Only the "
            "static risk rules are reported.",
            "",
        ]
    faults, clean = summary["faults"], summary["clean_controls"]
    lines += [
        "| | Static risk rules | Change Analyst (model) |",
        "| --- | --- | --- |",
    ]
    rows = (
        ("Faults anticipated (a risk pointing at the family)", "faults_anticipated", faults),
        ("Faults with a relevant edge case", "relevant_edge_cases", faults),
        ("Clean controls alarmed", "clean_controls_alarmed", clean),
        ("Risks flagged, all cases", "risks_flagged", None),
    )
    for title, key, total in rows:
        cells = []
        for arm in ("rules", "model"):
            if arm == "model" and not model_ran:
                cells.append("not run")
                continue
            value = summary[arm][key]
            cells.append(f"{value} of {total}" if total is not None else str(value))
        lines.append(f"| {title} | {cells[0]} | {cells[1]} |")
    lines += [
        "",
        "## Cases",
        "",
        "| Case | Family | Kind | Contracts fail | Rules matched | Rules: anticipated or "
        "alarmed | Model |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in document["cases"]:
        rules = case["rules"]
        verdict = rules["anticipated"] if case["kind"] == "fault" else rules["alarmed"]
        word = {True: "yes", False: "no", None: "n/a"}[verdict]
        model = case["model"]
        if model is None:
            model_cell = case["skipped"] or "not run"
        else:
            model_verdict = model["anticipated"] if case["kind"] == "fault" else model["alarmed"]
            model_cell = (
                f"{model['status']}; " + {True: "yes", False: "no", None: "n/a"}[model_verdict]
            )
        matched = ", ".join(f"`{m}`" for m in rules["matched"]) or "none"
        lines.append(
            f"| `{case['case_id']}` | {case['family']} | {case['kind']} | "
            f"{'yes' if case['detected_by_contracts'] else 'no'} | {matched} | {word} | "
            f"{model_cell} |"
        )
    lines += [
        "",
        "## Reading it",
        "",
        "- **What the rules are:** patterns that often go wrong in data migrations, matched in "
        "the changed source. A match is a reason to look, not a fault: a rule cannot tell a "
        "correct ranged backfill from one that skips its boundary.",
        "- **Optimistic recall:** the rules were written by the corpus's author with the corpus "
        "in view. They are generic, with no case-specific literal, but their recall here is "
        "an upper bound.",
        "- **Coverage:** what neither arm sees is out of reach. The rules read Python and SQL in "
        "the changed migration and application files, never the bindings.",
        "",
    ]
    return "\n".join(lines)
