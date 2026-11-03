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

**Scoring**, frozen here before any live run (docs/AI_EVALUATION.md):

* status: completed (with every citation valid), budget exhausted, invalid output or unavailable;
* localization: how many of the labelled affected event IDs the hypotheses name;
* root cause: whether the hypotheses or suggested action use the family's rubric terms
  (``RUBRIC``), and whether the first hypothesis alone does. This is a keyword proxy, so every
  hypothesis is kept verbatim for reading; how many other families' rubrics also match is
  reported as a sign of hedging;
* citation validity: of every evidence ID the final answer cites, how many exist. It is counted
  from the model's raw answer, so it is measured even when the investigator rejects the answer;
* unsupported specifics (``specifics``): numbers and identifiers in a hypothesis that appear in
  none of the evidence it cites;
* latency of both arms, and cost with cache writes and reads.

The label-free measures are taken as each case is investigated, from the transcript the model
saw (``Recorder``); labels are read only after every case has been investigated.
"""

import hashlib
import io
import json
import re
import statistics
import tarfile
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from commerce_scenario import PUBLISHED_DIGEST
from dataproof_benchmark.evaluate import contracts
from dataproof_benchmark.harness import SAMPLE_APP, DatabaseFactory, candidate, databases, reference
from dataproof_contracts import CheckOutcome, CheckResult, Environment, Investigation
from dataproof_investigator import (
    PROMPT_VERSION,
    TOOL_VERSION,
    Budget,
    Investigator,
    InvestigatorModel,
    ModelSession,
    ModelTurn,
    ScriptedModel,
    ToolContext,
    ToolResult,
    ToolSpec,
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

TRANSCRIPT_RESULT_CHARS = 2000
"""Tool results are kept in a transcript up to this length, with the full result's hash."""


@dataclass
class Recorder:
    """A model wrapper that keeps every turn of an investigation and every tool result sent.

    The investigator is unchanged: it sees the wrapped model's turns exactly. The recording is
    what the label-free measures are taken from, and what is published as the transcript.
    """

    model: InvestigatorModel
    turns: list[dict[str, Any]] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.model.name

    def open(
        self,
        *,
        system: str,
        task: str,
        tools: Sequence[ToolSpec],
        output_schema: Mapping[str, Any],
    ) -> ModelSession:
        session = self.model.open(
            system=system, task=task, tools=tools, output_schema=output_schema
        )
        return _RecordingSession(self, session)

    def tool_evidence(self) -> dict[str, str]:
        """Evidence ID to the full result of each tool call that produced evidence."""
        found = {}
        for result in self.results:
            if result.is_error:
                continue
            evidence_id = json.loads(result.content).get("evidence_id")
            if isinstance(evidence_id, str):
                found[evidence_id] = result.content
        return found

    def final_text(self) -> str | None:
        """The model's final answer as it was sent, before the investigator checked it."""
        ends = [t for t in self.turns if t.get("stop") == "end"]
        return ends[-1]["text"] if ends else None

    def cache_tokens(self) -> tuple[int, int]:
        """Tokens written to and read from the prompt cache, over every turn."""
        return (
            sum(t.get("cache_write_tokens", 0) for t in self.turns),
            sum(t.get("cache_read_tokens", 0) for t in self.turns),
        )


class _RecordingSession:
    def __init__(self, recorder: Recorder, session: ModelSession) -> None:
        self._recorder = recorder
        self._session = session

    def ceiling_usd(self, tool_results: Sequence[ToolResult]) -> Decimal | None:
        return self._session.ceiling_usd(tool_results)

    def send(self, tool_results: Sequence[ToolResult]) -> ModelTurn:
        self._recorder.results.extend(tool_results)
        sent: dict[str, Any] = {"tool_results": [_kept(result) for result in tool_results]}
        started = time.monotonic()
        try:
            turn = self._session.send(tool_results)
        except Exception as exc:
            self._recorder.turns.append(sent | {"error": f"{type(exc).__name__}: {exc}"})
            raise
        self._recorder.turns.append(
            sent
            | {
                "stop": turn.stop,
                "text": turn.text,
                "tool_uses": [
                    {"name": u.name, "arguments": dict(u.arguments)} for u in turn.tool_uses
                ],
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "cache_write_tokens": turn.cache_write_tokens,
                "cache_read_tokens": turn.cache_read_tokens,
                "cost_usd": str(turn.cost_usd),
                "served_by": turn.served_by,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return turn


def _kept(result: ToolResult) -> dict[str, Any]:
    content = result.content
    return {
        "is_error": result.is_error,
        "bytes": len(content.encode()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content": content[:TRANSCRIPT_RESULT_CHARS],
        "truncated": len(content) > TRANSCRIPT_RESULT_CHARS,
    }


# Label-free measures ---------------------------------------------------------------------------

_SPECIFIC_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?!\w)")
_ANY_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _number(token: str) -> str:
    return format(Decimal(token).normalize(), "f")


def specifics(statement: str) -> set[str]:
    """What a hypothesis states that evidence could show: numbers with three or more digits or
    a fractional part (event IDs, counts, amounts) and ``snake_case`` identifiers."""
    numbers = {
        _number(n)
        for n in _SPECIFIC_NUMBER.findall(statement)
        if "." in n or len(n.lstrip("0")) >= 3
    }
    return numbers | set(_IDENTIFIER.findall(statement))


def _found_in(text: str) -> set[str]:
    return {_number(n) for n in _ANY_NUMBER.findall(text)} | set(_IDENTIFIER.findall(text.lower()))


def evidence_texts(
    checks: Mapping[str, CheckResult],
    evidence: Mapping[str, Mapping[str, Any]],
    tool_evidence: Mapping[str, str],
) -> dict[str, str]:
    """Each citable evidence ID to what it shows: a run's evidence record with the checks that
    cite it, or a tool call's full result."""
    texts = {}
    for evidence_id, record in evidence.items():
        citing = [c.model_dump_json() for c in checks.values() if evidence_id in c.evidence_ids]
        texts[evidence_id] = json.dumps(record, default=str) + "\n" + "\n".join(citing)
    return texts | dict(tool_evidence)


def _answered(raw: str | None) -> list[dict[str, Any]] | None:
    """The hypotheses of a raw final answer, read leniently; None if it is not JSON."""
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("hypotheses"), list):
        return None
    return [
        {
            "statement": str(h.get("statement", "")),
            "cited_evidence_ids": [str(e) for e in h.get("cited_evidence_ids") or []],
        }
        for h in data["hypotheses"]
        if isinstance(h, dict)
    ]


def measures(
    investigation: Investigation, texts: Mapping[str, str], raw_answer: str | None = None
) -> dict[str, Any]:
    """Citation validity and unsupported specifics, from the answer as the model gave it
    (``raw_answer``) or, for the model-off arm, as recorded."""
    if raw_answer is not None:
        hypotheses = _answered(raw_answer)
    else:
        hypotheses = [h.model_dump(mode="json") for h in investigation.hypotheses]
    if hypotheses is None:
        return {"answer_parsed": False, "citations": 0, "valid_citations": 0}
    cited = [e for h in hypotheses for e in h["cited_evidence_ids"]]
    unsupported: list[str] = []
    stated = 0
    flagged = 0
    for hypothesis in hypotheses:
        shown: set[str] = set()
        for evidence_id in hypothesis["cited_evidence_ids"]:
            if evidence_id in texts:
                shown |= _found_in(texts[evidence_id])
        claimed = specifics(hypothesis["statement"])
        missing = sorted(claimed - shown)
        stated += len(claimed)
        flagged += bool(missing)
        unsupported += missing
    return {
        "answer_parsed": True,
        "citations": len(cited),
        "valid_citations": sum(e in texts for e in cited),
        "invalid_citations": sorted({e for e in cited if e not in texts}),
        "specifics": stated,
        "unsupported_specifics": unsupported,
        "hypotheses_with_unsupported": flagged,
    }


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
    started = time.perf_counter()
    model_off = diagnose(run_id, checks)
    latency = round((time.perf_counter() - started) * 1000, 3)
    row["model_off"] = model_off.model_dump(mode="json")
    row["model_off_measures"] = measures(model_off, evidence_texts(checks, evidence, {})) | {
        "latency_ms": latency,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }
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
    recorder = Recorder(model_for(checks))
    investigation = Investigator(recorder, BUDGET).investigate(run_id, tools)
    texts = evidence_texts(checks, evidence, recorder.tool_evidence())
    written, read = recorder.cache_tokens()
    row["model"] = investigation.model_dump(mode="json")
    # No final answer at all counts as an answer that did not parse.
    row["model_measures"] = measures(investigation, texts, recorder.final_text() or "") | {
        "latency_ms": investigation.usage.latency_ms,
        "cache_write_tokens": written,
        "cache_read_tokens": read,
    }
    row["transcript"] = recorder.turns
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


def other_rubrics(family: str, texts: Sequence[str]) -> int:
    """How many other families' rubrics the texts also match: a sign of hedging."""
    return sum(bool(rubric_match(other, texts)) for other in RUBRIC if other != family)


def score(
    investigation: Mapping[str, Any], measured: Mapping[str, Any], label: Mapping[str, Any]
) -> dict[str, Any]:
    texts = _texts(investigation)
    family = str(label["family"])
    first = [investigation["hypotheses"][0]["statement"]] if investigation["hypotheses"] else []
    affected = set(label["affected_events"])
    usage = investigation["usage"]
    outcomes = [c["outcome"] for c in investigation["tool_calls"]]
    return {
        "status": investigation["status"],
        "model": investigation["model"],
        "hypotheses": len(investigation["hypotheses"]),
        "named_affected": len(mentioned_numbers(texts) & affected),
        "affected": len(affected),
        "root_cause": rubric_match(family, texts),
        "root_cause_first": rubric_match(family, first) if first else False,
        "other_rubrics": other_rubrics(family, texts),
        "answer_parsed": measured["answer_parsed"],
        "citations": measured["citations"],
        "valid_citations": measured["valid_citations"],
        "invalid_citations": measured.get("invalid_citations", []),
        "specifics": measured.get("specifics", 0),
        "unsupported_specifics": measured.get("unsupported_specifics", []),
        "hypotheses_with_unsupported": measured.get("hypotheses_with_unsupported", 0),
        "tool_calls": {o: outcomes.count(o) for o in sorted(set(outcomes))},
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_write_tokens": measured["cache_write_tokens"],
        "cache_read_tokens": measured["cache_read_tokens"],
        "cost_usd": usage["cost_usd"],
        "latency_ms": measured["latency_ms"],
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
                "model_off": (
                    score(row["model_off"], row["model_off_measures"], label)
                    if row["model_off"]
                    else None
                ),
                "model": (
                    score(row["model"], row["model_measures"], label) if row["model"] else None
                ),
            }
        )
    return out


def _spread(values: Sequence[float]) -> dict[str, float | None]:
    """Median, 90th percentile (nearest rank) and maximum."""
    if not values:
        return {"median": None, "p90": None, "max": None}
    ordered = sorted(values)
    rank = max(0, -(-9 * len(ordered) // 10) - 1)
    return {"median": statistics.median(ordered), "p90": ordered[rank], "max": ordered[-1]}


def summarize(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ("model_off", "model"):
        arm_rows = [c[arm] for c in cases if c[arm]]
        costs = [Decimal(r["cost_usd"]) for r in arm_rows]
        summary[arm] = {
            "investigations": len(arm_rows),
            "statuses": dict(sorted(Counter(r["status"] for r in arm_rows).items())),
            "completed": sum(r["status"] == "COMPLETED" for r in arm_rows),
            "root_cause": sum(bool(r["root_cause"]) for r in arm_rows),
            "root_cause_first": sum(bool(r["root_cause_first"]) for r in arm_rows),
            "hedged": sum(r["other_rubrics"] > 0 for r in arm_rows),
            "named_an_affected_event": sum(r["named_affected"] > 0 for r in arm_rows),
            "citations": sum(r["citations"] for r in arm_rows),
            "valid_citations": sum(r["valid_citations"] for r in arm_rows),
            "specifics": sum(r["specifics"] for r in arm_rows),
            "unsupported_specifics": sum(len(r["unsupported_specifics"]) for r in arm_rows),
            "hypotheses_with_unsupported": sum(r["hypotheses_with_unsupported"] for r in arm_rows),
            "tool_calls": sum(sum(r["tool_calls"].values()) for r in arm_rows),
            "input_tokens": sum(r["input_tokens"] for r in arm_rows),
            "cache_write_tokens": sum(r["cache_write_tokens"] for r in arm_rows),
            "cache_read_tokens": sum(r["cache_read_tokens"] for r in arm_rows),
            "output_tokens": sum(r["output_tokens"] for r in arm_rows),
            "cost_usd": str(sum(costs, Decimal(0))),
            "cost_usd_per_investigation": {
                "mean": str(sum(costs, Decimal(0)) / len(costs)) if costs else None,
                "max": str(max(costs)) if costs else None,
            },
            "latency_ms": _spread([float(r["latency_ms"]) for r in arm_rows]),
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


def live_model(effort: str = EFFORT) -> InvestigatorModel:
    """The pinned model: no fallbacks, so every turn has a cost ceiling; prompt caching on."""
    import anthropic

    from dataproof_investigator.anthropic_model import AnthropicModel

    return AnthropicModel(
        anthropic.Anthropic(), model=MODEL, effort=effort, max_tokens=MAX_TOKENS, fallbacks=False
    )


def transcripts(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Each investigated case's transcript, to publish beside the results."""
    return {row["case_id"]: row["transcript"] for row in rows if row.get("transcript")}


def results(
    rows: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    *,
    live: bool,
    budget_usd: Decimal,
    model: str,
    effort: str = EFFORT,
) -> dict[str, Any]:
    cases = scored(rows, labels)
    return {
        "tier": "B3, the AI ablation, at the database tier",
        "dry_run": not live,
        "model": model,
        "effort": effort,
        "max_tokens": MAX_TOKENS,
        "prompt_caching": "automatic, 5-minute entries",
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
        ("Root cause named (rubric, any text)", "root_cause"),
        ("Root cause in the first hypothesis", "root_cause_first"),
        ("Also matched another family's rubric", "hedged"),
        ("Named an affected event", "named_an_affected_event"),
        ("Citations that exist / cited", "citation_validity"),
        ("Unsupported specifics / stated", "unsupported"),
        ("Hypotheses with an unsupported specific", "hypotheses_with_unsupported"),
        ("Tool calls", "tool_calls"),
        ("Input tokens (all)", "input_tokens"),
        ("Of which cache writes / reads", "cache"),
        ("Output tokens", "output_tokens"),
        ("Cost (USD), total", "cost_usd"),
        ("Cost (USD) per investigation, mean / max", "cost_spread"),
        ("Latency (ms), median / p90 / max", "latency"),
    )
    for title, key in rows:
        cells = [_cell(summary[arm], key) for arm in ("model_off", "model")]
        lines.append(f"| {title} | {cells[0]} | {cells[1]} |")
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
            unsupported = scoredarm["unsupported_specifics"]
            lines.append(
                f"- **{title}:** {scoredarm['status']}; root cause {root}; named "
                f"{scoredarm['named_affected']} of {scoredarm['affected']} affected events; "
                f"{scoredarm['valid_citations']} of {scoredarm['citations']} citations exist; "
                f"{len(unsupported)} of {scoredarm['specifics']} specifics unsupported"
                + (f" ({', '.join(f'`{u}`' for u in unsupported[:8])})" if unsupported else "")
                + f"; {sum(scoredarm['tool_calls'].values())} tool calls; "
                f"{scoredarm['input_tokens']} in / {scoredarm['output_tokens']} out tokens; "
                f"{scoredarm['cost_usd']} USD; {scoredarm['latency_ms']} ms."
            )
            if scoredarm["failure"]:
                lines.append(f"  - Not shown to reviewers: {scoredarm['failure']}")
            lines += [f"  - {text}" for text in scoredarm["texts"]]
        lines.append("")
    return "\n".join(lines)


def _cell(arm: Mapping[str, Any], key: str) -> str:
    if key == "citation_validity":
        return f"{arm['valid_citations']} / {arm['citations']}"
    if key == "unsupported":
        return f"{arm['unsupported_specifics']} / {arm['specifics']}"
    if key == "cache":
        return f"{arm['cache_write_tokens']} / {arm['cache_read_tokens']}"
    if key == "cost_spread":
        spread = arm["cost_usd_per_investigation"]
        return f"{spread['mean']} / {spread['max']}" if spread["mean"] is not None else "n/a"
    if key == "latency":
        spread = arm["latency_ms"]
        if spread["median"] is None:
            return "n/a"
        return " / ".join(f"{spread[k]:g}" for k in ("median", "p90", "max"))
    return str(arm[key])
