"""An audit of the first live AI evaluation, recounted from its frozen outputs (``ai-eval-v1``).

The run of 2026-11-19 (docs/AI_EVALUATION.md) wrote three result files and twenty transcripts.
They are kept exactly as written: ``results/ai-eval-v1.manifest.json`` holds each file's SHA-256,
and a test fails if one of them changes. This module reads those files and nothing else. It
calls no model and no database, and it rescores nothing: the scores are the ones the frozen
harness wrote. What it adds is what the harness's own report does not show:

* **spend, reconciled three ways:** the sum of every turn's recorded cost, the sum of every
  case's, and each turn's cost recomputed from its token counts at the list prices;
* **every tool call the model asked for, and what became of it:** read by the model, run but
  never read (the investigation ended first), or never run; by tool, with each refusal and
  failure put in a category;
* **how each investigation ended,** and what its last batch of calls was;
* **who named the root cause,** case by case: the rules, the model, both or neither;
* **the Change Analyst stage,** whose model arm sent no request at all.

``python -m dataproof_benchmark ai-eval-audit`` writes ``AI_EVAL_AUDIT.md`` and
``AI_EVAL_TRACES.md`` (each investigation, turn by turn); ``--check`` verifies the manifest.
"""

import hashlib
import json
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

BASELINE = "ai-eval-v1"
RESULTS = Path(__file__).resolve().parents[2] / "results"
MANIFEST = f"{BASELINE}.manifest.json"
AUDIT_REPORT = "AI_EVAL_AUDIT.md"
TRACES_REPORT = "AI_EVAL_TRACES.md"

STAGES: tuple[tuple[str, str, str], ...] = (
    ("Pilot", "ablation-pilot-results.json", "ablation-pilot-transcripts"),
    ("Main", "ablation-results.json", "ablation-transcripts"),
)
"""Failure Investigator stages: title, results file, transcript folder."""
CHANGE_RESULTS = "change-analysis-live-results.json"

#: USD per million tokens of ``claude-opus-5-5`` on the day of the run: uncached input, cache
#: write (1.25 times input), cache read, output. A test holds these to the adapter's table.
INPUT_PRICE, WRITE_PRICE, READ_PRICE, OUTPUT_PRICE = (
    Decimal(4),
    Decimal(5),
    Decimal("0.20"),
    Decimal(20),
)

RECORDED = ("get_check_result", "get_evidence")
"""Tools that only return what the run had already recorded before the investigation began."""

#: What must never appear in a published output: a key, a local path, a connection string.
FORBIDDEN = re.compile(
    r"sk-ant-|[A-Za-z]:\\\\Users|/Users/|/home/|OneDrive|postgres(?:ql)?://|password"
    r"|127\.0\.0\.1|localhost",
    re.IGNORECASE,
)

SEEN = ("OK", "DENIED", "FAILED")
UNREAD = "run, result never read"
NOT_RUN = "never run"


def frozen_files(results: Path = RESULTS) -> list[Path]:
    """The outputs of the run, in a stable order."""
    files = [results / name for _, name, _ in STAGES] + [results / CHANGE_RESULTS]
    for _, _, folder in STAGES:
        files += sorted((results / folder).glob("case-*.json"))
    return sorted(files)


def digests(results: Path = RESULTS) -> dict[str, str]:
    """Each frozen file's SHA-256, by its path under ``results``."""
    return {
        path.relative_to(results).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in frozen_files(results)
    }


def changed(results: Path = RESULTS) -> list[str]:
    """Frozen files that differ from the manifest, are missing from it, or are gone."""
    recorded: dict[str, str] = json.loads((results / MANIFEST).read_text("utf-8"))["files"]
    found = digests(results)
    return sorted(
        name for name in recorded.keys() | found.keys() if recorded.get(name) != found.get(name)
    )


def forbidden_content(results: Path = RESULTS) -> list[str]:
    """Frozen files holding something that must not be published (none is expected)."""
    return [
        path.relative_to(results).as_posix()
        for path in frozen_files(results)
        if FORBIDDEN.search(path.read_text("utf-8"))
    ]


def load(results: Path = RESULTS) -> dict[str, Any]:
    """The frozen documents: each stage's results with its transcripts, and the change run."""
    stages = []
    for title, name, folder in STAGES:
        document = json.loads((results / name).read_text("utf-8"))
        transcripts = {
            path.stem: json.loads(path.read_text("utf-8"))
            for path in sorted((results / folder).glob("case-*.json"))
        }
        stages.append({"title": title, "document": document, "transcripts": transcripts})
    change = json.loads((results / CHANGE_RESULTS).read_text("utf-8"))
    return {"stages": stages, "change": change}


def turn_cost(turn: Mapping[str, Any]) -> Decimal:
    """A turn's cost from its token counts alone, at the list prices."""
    written, read = turn["cache_write_tokens"], turn["cache_read_tokens"]
    fresh = turn["input_tokens"] - written - read
    total: Decimal = (
        INPUT_PRICE * fresh
        + WRITE_PRICE * written
        + READ_PRICE * read
        + OUTPUT_PRICE * turn["output_tokens"]
    )
    return total / Decimal(1_000_000)


def calls(turns: Sequence[Mapping[str, Any]], run: int) -> list[dict[str, Any]]:
    """Every tool call the model asked for, in order, with what became of it.

    A turn's tool results answer the turn before it. The last batch of an investigation that
    was stopped has no results in the transcript: of its calls, the investigator ran those up
    to its limit (``run`` is how many calls it recorded) and the model never read them; the
    rest never ran.
    """
    found: list[dict[str, Any]] = []
    for index, turn in enumerate(turns):
        answers = turns[index + 1]["tool_results"] if index + 1 < len(turns) else []
        for position, use in enumerate(turn.get("tool_uses", [])):
            call: dict[str, Any] = {
                "turn": index + 1,
                "tool": use["name"],
                "arguments": use["arguments"],
                "detail": "",
            }
            if position < len(answers):
                answer = answers[position]
                content = answer["content"]
                if not answer["is_error"]:
                    call["state"] = "OK"
                else:
                    call["state"] = "DENIED" if content.startswith('{"denied"') else "FAILED"
                    call["detail"] = str(next(iter(json.loads(content).values())))
            else:
                call["state"] = UNREAD if len(found) < run else NOT_RUN
            found.append(call)
    return found


def category(call: Mapping[str, Any]) -> str:
    """Why a call was refused or failed, as a class of cause."""
    tool, detail = call["tool"], call["detail"]
    if tool == "read_source":
        if "not a normalized relative path" in detail or "outside the readable" in detail:
            return "`read_source` of a directory: there is no way to list files"
        if "does not exist at the head commit" in detail:
            return "`read_source` of a guessed file name that does not exist"
    if tool == "run_readonly_query":
        if "current_database()" in detail or "pg_get_userbyid" in detail:
            return "query listing tables through the catalog: refused"
        if "function not allowed" in detail:
            return "query using a function outside the binding allowlist"
        if "specified more than once" in detail:
            return "query with two output columns of the same name"
        if "does not exist" in detail:
            return "query of a guessed relation that does not exist"
        if "RESULT_LIMIT_EXCEEDED" in detail:
            return "query returning more rows than `max_rows`: failed, not truncated"
    return f"`{tool}`: other"


def named_by(case: Mapping[str, Any]) -> str:
    """Who named the root cause, by the frozen rubric."""
    rules, model = bool(case["model_off"]["root_cause"]), bool(case["model"]["root_cause"])
    if rules and model:
        return "both"
    if rules:
        return "rules only"
    return "model only" if model else "neither"


def case_audit(case: Mapping[str, Any], turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One investigated case: its calls, tokens, cost and how it ended."""
    model = case["model"]
    run = sum(model["tool_calls"].values())
    asked = calls(turns, run)
    read = [c for c in asked if c["state"] in SEEN]
    source = [c for c in read if c["tool"] == "read_source"]
    queries = [c for c in read if c["tool"] == "run_readonly_query"]
    requests = [(c["tool"], json.dumps(c["arguments"], sort_keys=True)) for c in asked]
    return {
        "case_id": case["case_id"],
        "family": case["family"],
        "variant": case["variant"],
        "holdout": case["holdout"],
        "failed": [check.split(".")[-1] for check in case["failed"]],
        "status": model["status"],
        "failure": model["failure"],
        "rules_status": case["model_off"]["status"],
        "rules_root_cause": bool(case["model_off"]["root_cause"]),
        "model_root_cause": bool(model["root_cause"]),
        "named_by": named_by(case),
        "calls": asked,
        "turns": len(turns),
        "requested": len(asked),
        "run": run,
        "read": len(read),
        "repeats": len(requests) - len(set(requests)),
        "recorded_fetches": sum(c["tool"] in RECORDED for c in read),
        "source_attempts": len(source),
        "source_ok": sum(c["state"] == "OK" for c in source),
        "queries_ok": sum(c["state"] == "OK" for c in queries),
        "queries_refused": sum(c["state"] != "OK" for c in queries),
        "max_context": max(t["input_tokens"] for t in turns),
        "input_tokens": model["input_tokens"],
        "cache_write_tokens": model["cache_write_tokens"],
        "cache_read_tokens": model["cache_read_tokens"],
        "output_tokens": model["output_tokens"],
        "cost": Decimal(model["cost_usd"]),
        "turn_costs": sum((Decimal(t["cost_usd"]) for t in turns), Decimal(0)),
        "recomputed": sum((turn_cost(t) for t in turns), Decimal(0)),
        "latency_ms": model["latency_ms"],
        "stops": Counter(t.get("stop", "error") for t in turns),
        "outputs": [t["output_tokens"] for t in turns],
        "batches": Counter(len(t.get("tool_uses", [])) for t in turns),
    }


def stage_audit(stage: Mapping[str, Any]) -> dict[str, Any]:
    """A Failure Investigator stage: its investigated cases and its totals."""
    document, transcripts = stage["document"], stage["transcripts"]
    cases = [
        case_audit(case, transcripts[case["case_id"]])
        for case in document["cases"]
        if case["model"] is not None
    ]
    skipped = Counter(c["skipped"] for c in document["cases"] if c["model"] is None)
    return {
        "title": stage["title"],
        "document": document,
        "cases": cases,
        "skipped": dict(skipped),
        "turn_costs": sum((c["turn_costs"] for c in cases), Decimal(0)),
        "case_costs": sum((c["cost"] for c in cases), Decimal(0)),
        "recomputed": sum((c["recomputed"] for c in cases), Decimal(0)),
        "summary_cost": Decimal(document["summary"]["model"]["cost_usd"]),
    }


def change_audit(change: Mapping[str, Any]) -> dict[str, Any]:
    """The Change Analyst stage: what its model arm did, which is nothing."""
    models = [case["model"] for case in change["cases"] if case["model"] is not None]
    ceilings = sorted(
        {
            Decimal(found.group(1))
            for model in models
            if (found := re.search(r"could cost up to ([\d.]+) USD", model["failure"] or ""))
        }
    )
    return {
        "cases": len(change["cases"]),
        "analyzed": len(models),
        "statuses": dict(sorted(Counter(m["status"] for m in models).items())),
        "failures": dict(sorted(Counter(str(m["failure"]) for m in models).items())),
        "turns": sum(len(turns) for turns in change["transcripts"].values()),
        "cost": sum((Decimal(m["cost_usd"]) for m in models), Decimal(0)),
        "cap": Decimal(change["budget"]["case_cap_usd"]),
        "ceilings": ceilings,
        "summary": change["summary"],
    }


# Rendering -------------------------------------------------------------------------------------


def _usd(value: Decimal) -> str:
    return f"{value:.7f}".rstrip("0").rstrip(".") if value else "0"


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return [*lines, ""]


def _yes(value: object) -> str:
    return "yes" if value else "no"


def _spend(stages: Sequence[Mapping[str, Any]], change: Mapping[str, Any]) -> list[str]:
    rows = []
    for stage in stages:
        document = stage["document"]
        cases = stage["cases"]
        rows.append(
            (
                f"{stage['title']}, effort {document['effort']}",
                len(cases),
                sum(c["turns"] for c in cases),
                _usd(stage["turn_costs"]),
                _usd(stage["case_costs"]),
                _usd(stage["summary_cost"]),
                _usd(stage["recomputed"]),
            )
        )
    rows.append(
        ("Change Analyst", change["analyzed"], change["turns"], *(_usd(change["cost"]),) * 4)
    )
    total = sum((s["turn_costs"] for s in stages), Decimal(0)) + change["cost"]
    recomputed = sum((s["recomputed"] for s in stages), Decimal(0)) + change["cost"]
    agree = all(
        s["turn_costs"] == s["case_costs"] == s["summary_cost"] == s["recomputed"] for s in stages
    )
    lines = [
        "## 1. Stages and spend",
        "",
        "Cost in USD, counted four ways: the sum of every turn's recorded cost, the sum of every "
        "case's, the stage's own summary, and every turn recomputed from its token counts at "
        f"{INPUT_PRICE} (uncached input), {WRITE_PRICE} (cache write), {READ_PRICE} (cache read) "
        f"and {OUTPUT_PRICE} (output) USD per million tokens.",
        "",
        *_table(
            (
                "Stage",
                "Cases with a model arm",
                "Turns sent",
                "Cost: sum of turns",
                "Cost: sum of cases",
                "Cost: stage summary",
                "Cost: recomputed from tokens",
            ),
            rows,
        ),
        f"- **Total recorded:** {_usd(total)} USD. Recomputed from tokens: {_usd(recomputed)} USD.",
        "- **The four counts agree exactly in every stage.**"
        if agree
        else "- **The four counts do not agree:** see the table.",
        "- These are the harness's records of what the API reported. The provider's own usage "
        "page is the only independent record of what was billed, and only the account owner "
        "can read it.",
        "",
    ]
    token_rows = []
    for stage in stages:
        cases = stage["cases"]
        every = sum(c["input_tokens"] for c in cases)
        written = sum(c["cache_write_tokens"] for c in cases)
        read = sum(c["cache_read_tokens"] for c in cases)
        token_rows.append(
            (
                stage["title"],
                every,
                every - written - read,
                written,
                read,
                sum(c["output_tokens"] for c in cases),
            )
        )
    lines += _table(
        ("Stage", "Input tokens, all", "Uncached", "Cache writes", "Cache reads", "Output tokens"),
        token_rows,
    )
    return lines


def _scorecard(stage: Mapping[str, Any]) -> list[str]:
    summary = stage["document"]["summary"]
    rows = []
    for title, key in (
        ("Investigations", "investigations"),
        ("Completed, every citation valid", "completed"),
        ("Ended `BUDGET_EXHAUSTED`", "exhausted"),
        ("Root cause named (rubric, any text)", "root_cause"),
        ("Root cause in the first hypothesis", "root_cause_first"),
        ("Also matched another family's rubric", "hedged"),
        ("Named an affected event", "named_an_affected_event"),
        ("Citations that exist / cited", "citations"),
        ("Unsupported specifics / stated", "specifics"),
        ("Tool calls run", "tool_calls"),
        ("Cost, USD", "cost_usd"),
        ("Latency, ms: median / p90 / max", "latency_ms"),
    ):
        cells = []
        for arm in ("model_off", "model"):
            data = summary[arm]
            if key == "exhausted":
                cells.append(str(data["statuses"].get("BUDGET_EXHAUSTED", 0)))
            elif key == "citations":
                cells.append(f"{data['valid_citations']} / {data['citations']}")
            elif key == "specifics":
                cells.append(f"{data['unsupported_specifics']} / {data['specifics']}")
            elif key == "latency_ms":
                cells.append(" / ".join(f"{data[key][k]:g}" for k in ("median", "p90", "max")))
            elif key == "cost_usd":
                cells.append(_usd(Decimal(data[key])))
            else:
                cells.append(str(data[key]))
        rows.append((title, *cells))
    document = stage["document"]
    return [
        f"### {stage['title']}: effort {document['effort']}, {len(stage['cases'])} investigations",
        "",
        *_table(("Measure", "Rules (model-off)", "Model"), rows),
    ]


def _comparison(stage: Mapping[str, Any]) -> list[str]:
    cases = stage["cases"]
    rows = [
        (
            f"`{c['case_id']}`",
            f"{c['family']} ({c['variant']})",
            _yes(c["holdout"]),
            ", ".join(c["failed"]),
            f"{c['rules_status']}; {_yes(c['rules_root_cause'])}",
            f"{c['status']}; {_yes(c['model_root_cause'])}",
            c["named_by"],
        )
        for c in cases
    ]
    tally = Counter(c["named_by"] for c in cases)
    return [
        *_table(
            (
                "Case",
                "Fault family (variant)",
                "Held out",
                "Failed checks",
                "Rules: status; root cause",
                "Model: status; root cause",
                "Named by",
            ),
            rows,
        ),
        "Who named the root cause, by the frozen keyword rubric: "
        + "; ".join(
            f"**{name}** {tally.get(name, 0)}"
            for name in ("model only", "rules only", "both", "neither")
        )
        + f" (of {len(cases)}).",
        "",
    ]


def _tools(stage: Mapping[str, Any]) -> list[str]:
    cases = stage["cases"]
    every = [(c["case_id"], call) for c in cases for call in c["calls"]]
    states = Counter(call["state"] for _, call in every)
    read = sum(states[s] for s in SEEN)
    recorded = sum(c["recorded_fetches"] for c in cases)
    source_attempts = sum(c["source_attempts"] for c in cases)
    source_ok = sum(c["source_ok"] for c in cases)
    wanted_source = sum(any(x["tool"] == "read_source" for x in c["calls"]) for c in cases)
    stops: Counter[str] = sum((c["stops"] for c in cases), Counter())
    batches: Counter[int] = sum((c["batches"] for c in cases), Counter())
    outputs = sorted(n for c in cases for n in c["outputs"])
    recorded_outcomes: Counter[str] = Counter()
    for case in stage["document"]["cases"]:
        if case["model"] is not None:
            recorded_outcomes.update(case["model"]["tool_calls"])
    tools = sorted({call["tool"] for _, call in every})
    by_tool = [
        (
            f"`{tool}`",
            *(sum(call["tool"] == tool and call["state"] == s for _, call in every) for s in SEEN),
            sum(call["tool"] == tool and call["state"] == UNREAD for _, call in every),
            sum(call["tool"] == tool and call["state"] == NOT_RUN for _, call in every),
        )
        for tool in tools
    ]
    causes: dict[str, list[str]] = {}
    for case_id, call in every:
        if call["state"] in ("DENIED", "FAILED"):
            causes.setdefault(category(call), []).append(case_id)
    cause_rows = [
        (cause, len(ids), len(set(ids)))
        for cause, ids in sorted(causes.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    refused = states["DENIED"] + states["FAILED"]
    useful = sum(call["state"] == "OK" and call["tool"] not in RECORDED for _, call in every)
    lines = [
        f"### Tool calls: {stage['title'].lower()}",
        "",
        f"- **Asked for:** {len(every)} calls in {sum(c['turns'] for c in cases)} turns. "
        f"**Run:** {sum(c['run'] for c in cases)} (the results file records "
        + ", ".join(f"{n} {s}" for s, n in sorted(recorded_outcomes.items()))
        + f"). **Read by the model:** {read}. **Run, result never read:** {states[UNREAD]}. "
        f"**Never run:** {states[NOT_RUN]}.",
        f"- **Refused or failed, of the {read} the model read:** {refused} "
        f"({states['DENIED']} denied, {states['FAILED']} failed), or {_share(refused, read)}.",
        f"- **Re-reading what the run had already recorded** (`get_check_result`, "
        f"`get_evidence`): {recorded} calls, or {_share(recorded, read)} of those read.",
        f"- **So {useful} of the {read} calls the model read ({_share(useful, read)}) returned "
        "something the run had not already recorded:** a schema, a query result or source.",
        f"- **Source:** {wanted_source} of {len(cases)} investigations asked for `read_source`; "
        f"{source_attempts} calls were answered, and {source_ok} returned source.",
        f"- **Exact repeats** (same tool, same arguments): {sum(c['repeats'] for c in cases)}.",
        "- **Stop reasons, by turn:** "
        + ", ".join(f"{n} `{s}`" for s, n in sorted(stops.items()))
        + ". Refusals: "
        + f"{stops.get('refusal', 0)}; cut off at the token limit: {stops.get('max_tokens', 0)}; "
        f"API errors: {stops.get('error', 0)}.",
        "- **Turns, by how many calls each asked for:** "
        + ", ".join(f"{n} with {size}" for size, n in sorted(batches.items()))
        + ".",
        f"- **Output tokens in a turn** (thinking included): minimum {outputs[0]}, median "
        f"{statistics.median(outputs):g}, maximum {outputs[-1]}.",
        "",
        *_table(
            ("Tool", "OK", "Denied", "Failed", "Run, never read", "Never run"),
            by_tool,
        ),
    ]
    if cause_rows:
        lines += _table(("Why a call was refused or failed", "Calls", "Investigations"), cause_rows)
    return lines


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "n/a"


def _usage(stage: Mapping[str, Any]) -> list[str]:
    rows = [
        (
            f"`{c['case_id']}`",
            c["turns"],
            f"{c['requested']} / {c['run']} / {c['read']}",
            c["recorded_fetches"],
            f"{c['source_ok']} of {c['source_attempts']}",
            f"{c['queries_ok']} / {c['queries_refused']}",
            c["max_context"],
            c["input_tokens"],
            f"{c['cache_write_tokens']} / {c['cache_read_tokens']}",
            c["output_tokens"],
            _usd(c["cost"]),
            c["latency_ms"],
        )
        for c in stage["cases"]
    ]
    return [
        f"### Use: {stage['title'].lower()}",
        "",
        *_table(
            (
                "Case",
                "Turns",
                "Calls: asked / run / read",
                "Recorded results re-read",
                "Source reads that returned source",
                "Queries: OK / refused or failed",
                "Largest prompt, tokens",
                "Input tokens, all turns",
                "Cache writes / reads",
                "Output tokens",
                "Cost, USD",
                "Latency, ms",
            ),
            rows,
        ),
    ]


def _endings(stage: Mapping[str, Any]) -> list[str]:
    rows = []
    for c in stage["cases"]:
        last = [call for call in c["calls"] if call["turn"] == c["turns"]]
        before = c["requested"] - len(last)
        batch = ", ".join(f"`{call['tool']}`" for call in last) or "none: it answered"
        rows.append(
            (
                f"`{c['case_id']}`",
                c["status"],
                c["turns"],
                before,
                batch,
                c["failure"] or "an answer whose every citation exists",
            )
        )
    return [
        f"### Endings: {stage['title'].lower()}",
        "",
        *_table(
            (
                "Case",
                "Status",
                "Last turn",
                "Calls before it",
                "Asked for in the last turn",
                "How it ended",
            ),
            rows,
        ),
    ]


def _change(change: Mapping[str, Any]) -> list[str]:
    rules, model = change["summary"]["rules"], change["summary"]["model"]
    ceilings = ", ".join(_usd(c) for c in change["ceilings"]) or "none recorded"
    lines = [
        "## 6. The Change Analyst stage",
        "",
        f"- **Cases:** {change['cases']}; with a model arm: {change['analyzed']}.",
        "- **Model statuses:** "
        + ", ".join(f"{n} `{s}`" for s, n in change["statuses"].items())
        + ".",
        f"- **Turns sent to the model:** {change['turns']}. **Cost:** {_usd(change['cost'])} USD.",
        "- **Why each stopped:**",
    ]
    lines += [f'  - {n} times: "{text}"' for text, n in change["failures"].items()]
    lines += [
        f"- **The cap could not be met:** the most a first turn could cost was {ceilings} USD, "
        f"and the cap for a whole analysis was {_usd(change['cap'])} USD, so no turn was sent.",
        f"- **So the model arm's scores are not measurements.** The results file records "
        f"{model['faults_anticipated']} of {change['summary']['faults']} faults anticipated and "
        f"{model['clean_controls_alarmed']} of {change['summary']['clean_controls']} clean "
        "controls alarmed for the model. Both are the absence of an answer, not an answer.",
        f"- **The rules arm,** which ran: {rules['faults_anticipated']} of "
        f"{change['summary']['faults']} faults anticipated, {rules['clean_controls_alarmed']} of "
        f"{change['summary']['clean_controls']} clean controls alarmed, "
        f"{rules['risks_flagged']} risks flagged. These equal the committed "
        "`change-analysis-results.json`.",
        "",
    ]
    return lines


def report(data: Mapping[str, Any], unchanged: bool, clean: bool) -> str:
    """The audit, as Markdown."""
    stages = [stage_audit(stage) for stage in data["stages"]]
    change = change_audit(data["change"])
    main = stages[-1]
    lines = [
        "# AI evaluation v1: an audit of the frozen outputs",
        "",
        "Generated by `python -m dataproof_benchmark ai-eval-audit` from the frozen outputs of "
        "the live run of 2026-11-19. Do not edit by hand. It recounts; it rescores nothing. The "
        "reading of these numbers is in "
        "[`docs/AI_EVALUATION_RESULTS.md`](../../docs/AI_EVALUATION_RESULTS.md), and each "
        "investigation turn by turn is in [`AI_EVAL_TRACES.md`](AI_EVAL_TRACES.md).",
        "",
        *_spend(stages, change),
        "## 2. Scorecard, as the frozen harness scored it",
        "",
        "A stopped investigation has no answer, so it scores as not naming the root cause. "
        "That is a fact about the loop that stopped it as much as about the model: see "
        "sections 4 and 5.",
        "",
    ]
    for stage in stages:
        lines += _scorecard(stage)
    lines += [
        "## 3. The model against the rules, case by case (main run)",
        "",
        *_comparison(main),
        "## 4. Tool calls",
        "",
    ]
    for stage in stages:
        lines += _tools(stage)
    lines += ["## 5. Each investigation: use, and how it ended", ""]
    for stage in stages:
        lines += _usage(stage)
    for stage in stages:
        lines += _endings(stage)
    lines += _change(change)
    skipped = "; ".join(f"{n} cases: {why}" for why, n in main["skipped"].items()) or "none"
    frozen = sum(len(stage["transcripts"]) for stage in data["stages"]) + len(STAGES) + 1
    lines += [
        "## 7. Coverage and integrity",
        "",
        f"- **Not investigated in the main run:** {skipped}.",
        f"- **Frozen files:** {frozen} (three result files and the transcripts). They "
        + ("match" if unchanged else "DO NOT match")
        + f" the SHA-256 digests in `{MANIFEST}`.",
        "- **Content scan** for API keys, local paths, host names and connection strings: "
        + ("nothing found." if clean else "SOMETHING WAS FOUND; do not publish."),
        "",
    ]
    return "\n".join(lines)


def _argument(call: Mapping[str, Any]) -> str:
    arguments = call["arguments"]
    tool = call["tool"]
    if tool == "read_source":
        lines = f"{arguments.get('start_line')}-{arguments.get('end_line')}"
        text = f"{arguments.get('path')} lines {lines}"
    elif tool == "run_readonly_query":
        sql = " ".join(str(arguments.get("sql", "")).split())
        text = f"{arguments.get('environment')}: {sql[:220]}" + ("..." if len(sql) > 220 else "")
    else:
        text = ", ".join(str(value) for value in arguments.values())
    return text.replace("`", "'")


def traces(data: Mapping[str, Any]) -> str:
    """Every investigation turn by turn: what the model asked for and what came back."""
    lines = [
        "# AI evaluation v1: every investigation, turn by turn",
        "",
        "Generated by `python -m dataproof_benchmark ai-eval-audit` from the frozen transcripts. "
        "Do not edit by hand. Each turn lists the tool calls the model asked for and what became "
        "of each. The full transcripts, with tool results (to 2,000 characters, and the whole "
        "result's hash), token counts and costs, are the JSON files beside this one.",
        "",
    ]
    for stage in (stage_audit(s) for s in data["stages"]):
        document = stage["document"]
        lines += [f"## {stage['title']} (effort {document['effort']})", ""]
        texts = {c["case_id"]: c["model"]["texts"] for c in document["cases"] if c["model"]}
        for case in stage["cases"]:
            lines += [
                f"### {stage['title']}: `{case['case_id']}`, {case['family']} ({case['variant']})",
                "",
                f"Failed checks: {', '.join(case['failed'])}. Ended **{case['status']}**"
                + (f" ({case['failure']})" if case["failure"] else "")
                + f" after {case['turns']} turns; {case['requested']} calls asked for, "
                f"{case['run']} run, {case['read']} read. Root cause named by: {case['named_by']}.",
                "",
            ]
            for turn in range(1, case["turns"] + 1):
                asked = [call for call in case["calls"] if call["turn"] == turn]
                lines.append(f"- **Turn {turn}** ({case['outputs'][turn - 1]} output tokens)")
                for call in asked:
                    state = call["state"] + (f": {call['detail']}" if call["detail"] else "")
                    lines.append(
                        f"  - `{call['tool']}` `{_argument(call)}`: {state.replace('`', chr(39))}"
                    )
                if not asked:
                    lines.append("  - the final answer, below")
            lines.append("")
            if texts[case["case_id"]]:
                lines += ["The answer, verbatim (hypotheses, then the suggested action):", ""]
                lines += ["\n>\n".join(f"> {text}" for text in texts[case["case_id"]]), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write(results: Path = RESULTS) -> None:
    """Write both reports beside the frozen outputs."""
    data = load(results)
    audit = report(data, unchanged=not changed(results), clean=not forbidden_content(results))
    (results / AUDIT_REPORT).write_text(audit, "utf-8", newline="\n")
    (results / TRACES_REPORT).write_text(traces(data), "utf-8", newline="\n")
