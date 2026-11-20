"""The second AI evaluation's sets, graders and harness (docs/AI_EVALUATION_V2.md).

**Nothing here has been run against a paid model.** A live run needs the owner's approval of
the protocol and of a budget; without ``--live`` a scripted model runs every path at no cost.

What the first evaluation taught (docs/AI_EVALUATION_RESULTS.md) is built in:

* **Two new sets.** The first evaluation's cases have been read closely, so they are retired.
  ``DevSet`` is the commerce generator with a new seed: the same fault templates, new
  instances, for development. ``HeldoutSet`` is the subscriptions example: another schema, its
  fault patterns mostly taken from documented incidents, with every comment and docstring
  removed from what a model can read (``blind``), because they name the fault. It is run once,
  after the investigator is frozen, and never iterated on.
* **An investigation that was stopped is not a wrong answer.** Each is graded ``answered`` or
  not, and root cause and location are graded only on answers.
* **Atomic graders.** Besides the keyword rubric: ``located``, whether the answer states lines
  of the changed migration that overlap the statement at fault. It is mechanical and it is not
  a keyword.
* **Repeats, with intervals.** Every case can be investigated several times; rates carry
  Wilson intervals, and a case's verdict is the majority of its repeats.
* **A cap that cannot be met is an error before the run** (``ablation.require_feasible_cap``),
  and a model arm that sent no request is reported as not called, never as a score.

The harness uses only what the investigator package has offered since before ADR-0009, so the
same code runs the old investigator from a checkout of the old package: that is the old arm.
"""

import ast
import difflib
import io
import json
import math
import os
import re
import statistics
import tarfile
import time
import tokenize
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from dataproof_benchmark import ablation, harness
from dataproof_benchmark.ablation import Recorder, evidence_texts, measures, reader
from dataproof_benchmark.corpus import SAFE, build_corpus
from dataproof_benchmark.files import rendered
from dataproof_benchmark.harness import DatabaseFactory, databases
from dataproof_contracts import (
    CheckResult,
    Confidence,
    DataContract,
    Environment,
    Investigation,
    InvestigationStatus,
    InvestigationUsage,
)
from dataproof_investigator import (
    Budget,
    Investigator,
    InvestigatorModel,
    ScriptedModel,
    ToolContext,
    answer,
    call,
    change_analyst,
    diagnose,
    risk_rules,
)
from dataproof_verifier import (
    QueryLimits,
    assemble_contract,
    load_binding,
    load_oracle,
    type_check,
    verify_contract,
)

DEV_SEED = 20261119
"""The development set's seed: fixed here, different from the first evaluation's."""
EXAMPLE = harness.REPOSITORY / "examples" / "subscriptions"
HELDOUT_LABELS = Path(__file__).resolve().parents[2] / "heldout" / "labels.json"
"""The held-out set's evaluator manifest: read only after every case has been investigated,
except for the neutral case IDs and the change set each stands for."""
HELDOUT_REVISION = "migrations/versions/0002_change.py"
"""Where a held-out case's revision is in the archive a model reads: a neutral name."""

MAX_LOCATED_WIDTH = 20
"""The widest stated line range that counts as locating a statement: naming a whole file as
"lines 1-200" locates nothing."""

ANALYST_MAX_TOKENS = 4000
ANALYST_CASE_CAP_USD = Decimal("0.25")
"""The Change Analyst's cap for one analysis. With 4,000 output tokens a turn its first turn
can cost at most about 0.12 USD, so the cap covers two worst-case turns. The first
evaluation's cap, 0.15 USD against a 0.194 USD turn, covered none."""
ANALYST_BUDGET = Budget(max_tool_calls=8, max_turns=10, max_cost_usd=ANALYST_CASE_CAP_USD)

ModelFactory = Callable[[], InvestigatorModel]
STOPPED = "the stop rule: too many investigations gave no answer"
HALTED = "halted: an earlier investigation of this stage showed a fault in the plumbing"
INTERRUPTED = (
    "interrupted: the process ended before the investigation did. It was started, so it is "
    "counted as it stands and is not sent again"
)


# A stage's records, as they happen -------------------------------------------------------------


class Journal:
    """A stage's records on disk as they happen, each file created once and never rewritten.

    ``<key>.turns.jsonl`` gets a line when an investigation starts, and one for each turn as
    soon as the turn's record exists, a failed request's included. ``<key>.run.json`` is
    written when the investigation has ended. So what a stage spent is on disk even if its
    process dies; the first evaluation wrote everything at the end of a stage, and a crash
    would have left its spend unknown.

    A stage taken up again from the same journal starts only what was never started. An
    investigation with a turns file and no run record was interrupted: it is counted as it
    stands, unanswered, and never sent again.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def _turns(self, key: str) -> Path:
        return self.folder / f"{key}.turns.jsonl"

    def _run(self, key: str) -> Path:
        return self.folder / f"{key}.run.json"

    def finished(self, key: str) -> dict[str, Any] | None:
        """The record of an investigation that ended, or None."""
        path = self._run(key)
        if not path.is_file():
            return None
        loaded: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return loaded

    def interrupted(self, key: str) -> list[dict[str, Any]] | None:
        """The turns of an investigation that started and never ended, or None."""
        path = self._turns(key)
        if not path.is_file() or self._run(key).is_file():
            return None
        turns = []
        for line in path.read_text("utf-8").splitlines()[1:]:
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:  # a line cut off as the process died
                turns.append({"error": "a turn's record was cut off as the process ended"})
        return turns

    def start(self, key: str) -> Callable[[Mapping[str, Any]], None]:
        """Mark an investigation started, which it must not have been, and return the
        function that writes each of its turns."""
        path = self._turns(key)
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps({"started": key, "at": datetime.now(UTC).isoformat()}) + "\n")

        def write(turn: Mapping[str, Any]) -> None:
            with path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(turn, default=str) + "\n")
                file.flush()
                os.fsync(file.fileno())

        return write

    def finish(self, key: str, run: Mapping[str, Any]) -> None:
        with self._run(key).open("x", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(run, indent=2, default=str) + "\n")
            file.flush()
            os.fsync(file.fileno())


@dataclass
class _Stage:
    """What a stage counts while it runs, and the rules that stop it starting another
    investigation. ``record`` is where a run keeps its result: an investigation or an analysis."""

    record: str
    budget_usd: Decimal
    cap: Decimal
    journal: Journal | None = None
    served_by: str | None = None
    """The pinned model of a live stage: a turn another model served halts the stage."""
    stop_after_unanswered: int | None = None
    spent: Decimal = Decimal(0)
    unanswered: int = 0
    halted: str | None = None
    counted: int = 0

    def blocked(self) -> str | None:
        """Why the next investigation is not started, or None."""
        if self.halted is not None:
            return f"{HALTED}: {self.halted}"
        if self.budget_usd - self.spent < self.cap:
            return "the budget left cannot cover it"
        if self.stop_after_unanswered is not None and self.unanswered >= self.stop_after_unanswered:
            return STOPPED
        return None

    def kept(self, key: str, rep: int, interrupted: Investigation) -> dict[str, Any] | None:
        """The record of an investigation that a run of this stage already started, which this
        one continues: it is counted, and nothing is sent. ``interrupted`` is the record it
        gets if it never ended. None if it was never started."""
        if self.journal is None:
            return None
        run = self.journal.finished(key)
        if run is None:
            turns = self.journal.interrupted(key)
            if turns is None:
                return None
            investigation = _with_usage(interrupted, turns)
            run = {
                "rep": rep,
                self.record: investigation.model_dump(mode="json"),
                "measures": measures(investigation, {}, "") | _turn_measures(turns),
                "transcript": turns,
                "interrupted": True,
            }
            self.journal.finish(key, run)
        self.count(key, run, check=False)
        return run

    def count(self, key: str, run: Mapping[str, Any], *, check: bool = True) -> None:
        """Count a finished run: its cost, whether it answered, and whether it shows a fault in
        the plumbing that must stop the stage (not for a run kept from before: taking the
        stage up again was a decision made knowing of it)."""
        record = run[self.record]
        self.counted += 1
        self.spent += Decimal(record["usage"]["cost_usd"])
        self.unanswered += record["status"] != InvestigationStatus.COMPLETED.value
        if check and self.halted is None:
            problems = health({key: run["transcript"]}, self.served_by)
            self.halted = problems[0] if problems else None
        say(
            f"[{self.counted}] {key}: {record['status']}, {len(run['transcript'])} turns, "
            f"{len(record['tool_calls'])} tool calls, {record['usage']['cost_usd']} USD, "
            f"{record['usage']['latency_ms'] / 1000:.1f} s; the stage has spent {self.spent} "
            f"of {self.budget_usd} USD" + (" (kept from before)" if not check else "")
        )


def say(text: str) -> None:
    """A line of progress, flushed: a paid stage is watched while it runs."""
    print(text, flush=True)


def _turn_measures(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "latency_ms": sum(int(t.get("latency_ms", 0)) for t in turns),
        "cache_write_tokens": sum(int(t.get("cache_write_tokens", 0)) for t in turns),
        "cache_read_tokens": sum(int(t.get("cache_read_tokens", 0)) for t in turns),
        "turns": len(turns),
    }


def _with_usage(investigation: Investigation, turns: Sequence[Mapping[str, Any]]) -> Investigation:
    """An interrupted investigation's record with what its recorded turns used."""
    usage = InvestigationUsage(
        input_tokens=sum(int(t.get("input_tokens", 0)) for t in turns),
        output_tokens=sum(int(t.get("output_tokens", 0)) for t in turns),
        cost_usd=sum((Decimal(str(t.get("cost_usd", "0"))) for t in turns), Decimal(0)),
        latency_ms=sum(int(t.get("latency_ms", 0)) for t in turns),
    )
    return investigation.model_copy(update={"usage": usage})


def _interrupted(investigator: Investigator, run_id: uuid.UUID) -> Investigation:
    from dataproof_investigator import TOOL_VERSION

    now = datetime.now(UTC)
    return Investigation(
        investigation_id=uuid.uuid4(),
        run_id=run_id,
        role=investigator.role,
        status=InvestigationStatus.NOT_AVAILABLE,
        model=investigator.model.name,
        prompt_version=investigator.prompt_version,
        tool_version=TOOL_VERSION,
        failure=INTERRUPTED,
        usage=InvestigationUsage(cost_usd=Decimal(0), latency_ms=0),
        started_at=now,
        finished_at=now,
    )


# Blinding and line ranges ----------------------------------------------------------------------


def blind(source: str) -> str:
    """A Python source file without its comments and docstrings; every other line unchanged.

    A revision's comments say what it does wrong ("integer / integer is integer division"), and
    an investigator that reads the answer has explained nothing. Statements keep their text, so
    a line range in the blinded file still names a statement.
    """
    tree = ast.parse(source)
    dropped: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and len(node.body) > 1
        ):
            dropped.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    comments = {
        token.start[0]: token.start[1]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
    kept = []
    for number, line in enumerate(source.splitlines(), 1):
        if number in dropped:
            continue
        if number in comments:
            line = line[: comments[number]].rstrip()
            if not line:
                continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n\n", "\n".join(kept).strip("\n") + "\n")


def statement_lines(source: str, markers: Sequence[str]) -> tuple[int, int] | None:
    """The lines of the statements that hold the markers: the smallest statement for each."""
    spans = []
    for marker in markers:
        found = [
            (node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.stmt) and marker in (ast.get_source_segment(source, node) or "")
        ]
        if not found:
            return None
        spans.append(min(found, key=lambda span: span[1] - span[0]))
    return (min(s for s, _ in spans), max(e for _, e in spans)) if spans else None


def changed_lines(safe: str, faulty: str) -> tuple[int, int] | None:
    """The lines of ``faulty`` that differ from ``safe``: where a generated fault is."""
    matcher = difflib.SequenceMatcher(a=safe.splitlines(), b=faulty.splitlines(), autojunk=False)
    differing = [
        line
        for tag, _, _, start, end in matcher.get_opcodes()
        if tag != "equal"
        for line in range(start + 1, max(end, start + 1) + 1)
    ]
    return (min(differing), max(differing)) if differing else None


_RANGE = re.compile(
    r"(?:\blines?\s+|\bL|\.py:)(\d{1,4})(?:\s*(?:-|\N{EN DASH}|to|through|and)\s*L?(\d{1,4}))?",
    re.IGNORECASE,
)


def stated_ranges(text: str) -> list[tuple[int, int]]:
    """Line ranges a text states: "lines 22-30", "line 25", "L22-L30", "0002_change.py:25"."""
    ranges = []
    for found in _RANGE.finditer(text):
        start = int(found.group(1))
        end = int(found.group(2)) if found.group(2) else start
        ranges.append((min(start, end), max(start, end)))
    return ranges


def located(texts: Sequence[str], fault: Sequence[int] | None) -> bool | None:
    """Whether the texts state lines that overlap the statement at fault, narrowly."""
    if fault is None:
        return None
    low, high = fault
    return any(
        start <= high and end >= low and end - start < MAX_LOCATED_WIDTH
        for text in texts
        for start, end in stated_ranges(text)
    )


def rubric(groups: Sequence[Sequence[str]] | None, texts: Sequence[str]) -> bool | None:
    """The keyword proxy for naming a cause: every group must match at least one term."""
    if groups is None:
        return None
    joined = " ".join(texts).lower()
    return all(any(term in joined for term in group) for group in groups)


def wilson(successes: int, trials: int) -> tuple[float, float] | None:
    """A 95% Wilson interval for a rate: honest at the small counts an evaluation has."""
    if trials == 0:
        return None
    z = 1.959964
    rate = successes / trials
    centre = rate + z * z / (2 * trials)
    spread = z * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials))
    scale = 1 + z * z / trials
    return (max(0.0, (centre - spread) / scale), min(1.0, (centre + spread) / scale))


# Sets ------------------------------------------------------------------------------------------


class EvalSet(Protocol):
    """Cases a harness can stage. ``labels`` is the evaluator's, read after investigating."""

    name: str
    source_roots: tuple[str, ...]
    migration_roots: tuple[str, ...]

    def case_ids(self) -> list[str]: ...
    def reference(self, new_database: DatabaseFactory) -> str: ...
    def candidate(self, new_database: DatabaseFactory, case_id: str) -> str: ...
    def verify(
        self, reference_dsn: str, candidate_dsn: str, case_id: str
    ) -> tuple[dict[str, CheckResult], dict[str, Mapping[str, Any]]]: ...
    def archive(self, case_id: str) -> bytes: ...
    def changed(self, case_id: str) -> dict[str, str]: ...
    def labels(self) -> dict[str, dict[str, Any]]: ...


class DevSet:
    """The commerce generator with a new seed: for development, never for a headline."""

    name = "dev"
    source_roots: tuple[str, ...] = ablation.SOURCE_ROOTS
    migration_roots: tuple[str, ...] = ("sample-app/migrations/",)

    def __init__(self, workdir: Path, seed: int = DEV_SEED) -> None:
        self.seed = seed
        self._cases = {c.case_id.replace("case-", "dev-"): c for c in build_corpus(seed)}
        self._root = workdir / f"corpus-dev-{seed}"
        for relative, text in rendered(list(self._cases.values()), []).items():
            path = self._root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8", newline="\n")

    def _directory(self, case_id: str) -> Path:
        return self._root / "changes" / self._cases[case_id].change

    def case_ids(self) -> list[str]:
        return sorted(self._cases)

    def reference(self, new_database: DatabaseFactory) -> str:
        return harness.reference(new_database)

    def candidate(self, new_database: DatabaseFactory, case_id: str) -> str:
        return harness.candidate(new_database, self._directory(case_id))

    def verify(
        self, reference_dsn: str, candidate_dsn: str, case_id: str
    ) -> tuple[dict[str, CheckResult], dict[str, Mapping[str, Any]]]:
        return ablation.checks_and_evidence(reference_dsn, candidate_dsn, self._directory(case_id))

    def archive(self, case_id: str) -> bytes:
        return ablation.source_archive(self._directory(case_id))

    def changed(self, case_id: str) -> dict[str, str]:
        case = self._cases[case_id]
        path = f"sample-app/migrations/versions/{case.revision}_ledger_backfill.py"
        return {path: case.source()}

    def labels(self) -> dict[str, dict[str, Any]]:
        found = {}
        for case_id, case in self._cases.items():
            safe = replace(case, statements=SAFE).source()
            found[case_id] = {
                "kind": case.label.kind,
                "family": case.label.family,
                "variant": case.label.variant,
                "origin": "generated",
                "rubric": ablation.RUBRIC.get(case.label.family),
                "fault_lines": changed_lines(safe, case.source())
                if case.label.kind == "fault"
                else None,
                "affected": list(case.label.affected_events),
            }
        return found


def _migrate_example(dsn: str, change: Path | None) -> None:
    config = Config()
    config.set_main_option("script_location", str(EXAMPLE / "migrations"))
    config.set_main_option("path_separator", "os")
    locations = [EXAMPLE / "migrations" / "versions"]
    if change is not None:
        locations.append(change)
    config.set_main_option("version_locations", os.pathsep.join(map(str, locations)))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn))
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


class HeldoutSet:
    """The subscriptions example, blinded: another schema, run once, never iterated on."""

    name = "heldout"
    source_roots: tuple[str, ...] = ("migrations/",)
    migration_roots: tuple[str, ...] = ("migrations/",)
    dataset_digest = "0" * 64

    def __init__(self) -> None:
        manifest = json.loads(HELDOUT_LABELS.read_text("utf-8"))
        self._labels: dict[str, dict[str, Any]] = {c["case_id"]: c for c in manifest["cases"]}

    def _directory(self, case_id: str) -> Path:
        return EXAMPLE / "changes" / str(self._labels[case_id]["change"])

    def _revision(self, case_id: str) -> str:
        (revision,) = sorted(self._directory(case_id).glob("*.py"))
        return blind(revision.read_text("utf-8"))

    def case_ids(self) -> list[str]:
        return sorted(self._labels)

    def reference(self, new_database: DatabaseFactory) -> str:
        dsn = new_database()
        _migrate_example(dsn, None)
        with psycopg.connect(dsn) as connection:
            connection.execute((EXAMPLE / "dataproof" / "fixture.sql").read_text("utf-8"))
            connection.commit()
        return dsn

    def candidate(self, new_database: DatabaseFactory, case_id: str) -> str:
        dsn = self.reference(new_database)
        _migrate_example(dsn, self._directory(case_id))
        return dsn

    def _contracts(self, case_id: str) -> list[DataContract]:
        contracts = []
        for oracle_path in sorted((EXAMPLE / "dataproof" / "oracles").glob("*.toml")):
            spec, oracle = load_oracle(oracle_path, source_path=oracle_path.name, base_sha="b" * 40)
            binding_path = self._directory(case_id) / "bindings" / oracle_path.name
            binding, approved = None, set()
            if binding_path.is_file():
                _, binding = load_binding(
                    binding_path, source_path=binding_path.name, head_sha="c" * 40
                )
                approved = {binding.digest()}
            contracts.append(assemble_contract(spec, oracle, binding, approved_digests=approved))
        return contracts

    def verify(
        self, reference_dsn: str, candidate_dsn: str, case_id: str
    ) -> tuple[dict[str, CheckResult], dict[str, Mapping[str, Any]]]:
        """The data contracts and their type checks, as ``VERIFYING`` records them."""
        checks: dict[str, CheckResult] = {}
        evidence: dict[str, Mapping[str, Any]] = {}
        with (
            psycopg.connect(reference_dsn, autocommit=True) as ref,
            psycopg.connect(candidate_dsn, autocommit=True) as cand,
        ):
            for contract in self._contracts(case_id):
                verification = verify_contract(
                    contract, ref, cand, self.dataset_digest, QueryLimits()
                )
                checks[verification.check.check_id] = verification.check
                typed = type_check(contract, verification)
                checks[typed.check_id] = typed
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

    def _files(self, case_id: str) -> dict[str, str]:
        base = EXAMPLE / "migrations"
        return {
            "migrations/env.py": blind((base / "env.py").read_text("utf-8")),
            "migrations/versions/0001_base.py": blind(
                (base / "versions" / "0001_base.py").read_text("utf-8")
            ),
            HELDOUT_REVISION: self._revision(case_id),
        }

    def archive(self, case_id: str) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, text in sorted(self._files(case_id).items()):
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def changed(self, case_id: str) -> dict[str, str]:
        return {HELDOUT_REVISION: self._revision(case_id)}

    def labels(self) -> dict[str, dict[str, Any]]:
        found = {}
        for case_id, label in self._labels.items():
            markers = label.get("fault_markers") or []
            found[case_id] = {
                "kind": label["kind"],
                "family": label["family"],
                "variant": label["change"],
                "origin": label["origin"],
                "rubric": label.get("rubric"),
                "fault_lines": statement_lines(self._revision(case_id), markers)
                if markers
                else None,
                "affected": [],
            }
        return found


def eval_set(name: str, workdir: Path) -> EvalSet:
    if name == "dev":
        return DevSet(workdir)
    if name == "heldout":
        return HeldoutSet()
    raise ValueError(f"there is no set named {name!r}")


# The Failure Investigator ----------------------------------------------------------------------


def dry_run_model() -> InvestigatorModel:
    """A scripted investigator with a real price: it reads one check, then cites what it read.

    It reports the least a live turn's ceiling can be, so that a cap no live turn could meet
    stops a dry run too. The first evaluation's dry run had no price and could not see one.
    """
    final = {
        "hypotheses": [
            {
                "statement": "Scripted: the first failed check was read.",
                "cited_evidence_ids": ["investigation.tool.1"],
                "confidence": "LOW",
            }
        ],
        "suggested_action": None,
        "caveat": "A dry run: no model was called.",
    }
    return _FirstCheckModel(json.dumps(final))


class _FirstCheckModel:
    """Scripted: call ``get_check_result`` on the first failed check named in the task."""

    name = "scripted:dry-run"

    def __init__(self, final: str) -> None:
        self._final = final

    def open(self, *, system: str, task: str, tools: Any, output_schema: Any) -> Any:
        found = re.search(r"^- (\S+): (?:FAIL|ERROR)", task, re.MULTILINE)
        first = call("get_check_result", {"check_id": found.group(1) if found else "none"})
        model = ScriptedModel(
            [first, answer(self._final)],
            name=self.name,
            turn_ceiling_usd=ablation.DRY_RUN_CEILING_USD,
        )
        return model.open(system=system, task=task, tools=tools, output_schema=output_schema)


def investigate(
    admin_dsn: str,
    cases: EvalSet,
    model_for: ModelFactory,
    budget_usd: Decimal,
    *,
    reps: int = 1,
    case_ids: Sequence[str] | None = None,
    skip: Sequence[str] = (),
    budget: Budget = ablation.BUDGET,
    stop_after_unanswered: int | None = None,
    journal: Journal | None = None,
    served_by: str | None = None,
) -> list[dict[str, Any]]:
    """Investigate every failing case ``reps`` times; labels are not read here.

    ``stop_after_unanswered`` is the stop rule: once that many investigations have given no
    answer, no further one is started. It needs no label: answering is not a label. A fault
    in the plumbing (no request sent, a failed request, a turn ``served_by`` did not serve)
    stops the stage at once. With a ``journal`` every turn is on disk as it happens, and what
    the journal already holds is counted and not sent again.
    """
    rows: list[dict[str, Any]] = []
    cap = budget.max_cost_usd or ablation.CASE_CAP_USD
    stage = _Stage(
        "investigation",
        budget_usd,
        cap,
        journal=journal,
        served_by=served_by,
        stop_after_unanswered=stop_after_unanswered,
    )
    checked = False
    with reader(admin_dsn) as as_reader, databases(admin_dsn) as new_database:
        reference_dsn = cases.reference(new_database)
        for case_id in cases.case_ids():
            if (case_ids and case_id not in case_ids) or case_id in skip:
                continue
            row: dict[str, Any] = {"case_id": case_id, "rules": None, "runs": [], "skipped": None}
            rows.append(row)
            try:
                candidate_dsn = cases.candidate(new_database, case_id)
            except Exception:  # a failing migration has no data checks to explain
                row["skipped"] = "the migration failed"
                continue
            checks, evidence = cases.verify(reference_dsn, candidate_dsn, case_id)
            row["failed"] = ablation.failed(checks)
            if not row["failed"]:
                row["skipped"] = "no check failed or errored"
                continue
            run_id = uuid.uuid4()
            started = time.perf_counter()
            rules = diagnose(run_id, checks)
            row["rules"] = rules.model_dump(mode="json")
            row["rules_measures"] = measures(rules, evidence_texts(checks, evidence, {})) | {
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
            }
            context = ToolContext(
                checks=checks,
                evidence=evidence,
                source_archive=cases.archive(case_id),
                source_roots=cases.source_roots,
                databases={
                    Environment.REFERENCE: as_reader(reference_dsn),
                    Environment.CANDIDATE: as_reader(candidate_dsn),
                },
                changed_paths=tuple(cases.changed(case_id)),
            )
            for rep in range(1, reps + 1):
                key = f"{case_id}.rep{rep}"
                recorder = Recorder(model_for())
                investigator = Investigator(recorder, budget)
                kept = stage.kept(key, rep, _interrupted(investigator, run_id))
                if kept is not None:
                    row["runs"].append(kept)
                    continue
                blocked = stage.blocked()
                if blocked is not None:
                    row["runs"].append({"rep": rep, "skipped": blocked})
                    continue
                if not checked:
                    ablation.require_feasible_cap(investigator, context, cap)
                    checked = True
                if journal is not None:
                    recorder.on_turn = journal.start(key)
                investigation = investigator.investigate(run_id, context)
                texts = evidence_texts(checks, evidence, recorder.tool_evidence())
                written, read = recorder.cache_tokens()
                measured = measures(investigation, texts, recorder.final_text() or "")
                run = {
                    "rep": rep,
                    "investigation": investigation.model_dump(mode="json"),
                    "measures": measured
                    | {
                        "latency_ms": investigation.usage.latency_ms,
                        "cache_write_tokens": written,
                        "cache_read_tokens": read,
                        "turns": len(recorder.turns),
                    },
                    "transcript": recorder.turns,
                }
                if journal is not None:
                    journal.finish(key, run)
                row["runs"].append(run)
                stage.count(key, run)
    return rows


def _texts(investigation: Mapping[str, Any]) -> list[str]:
    texts = [h["statement"] for h in investigation["hypotheses"]]
    action = investigation.get("suggested_action")
    return texts + ([action] if action else [])


def grade(
    investigation: Mapping[str, Any], measured: Mapping[str, Any], label: Mapping[str, Any]
) -> dict[str, Any]:
    """One investigation against its label. Causes and locations are graded on answers only:
    an investigation that gave none is ``answered: False``, and nothing more is claimed."""
    texts = _texts(investigation)
    answered = investigation["status"] == "COMPLETED"
    first = texts[:1]
    usage = investigation["usage"]
    outcomes = Counter(c["outcome"] for c in investigation["tool_calls"])
    affected = {int(n) for n in label.get("affected") or []}
    return {
        "status": investigation["status"],
        "answered": answered,
        "root_cause": rubric(label["rubric"], texts) if answered else None,
        "root_cause_first": rubric(label["rubric"], first) if answered else None,
        "located": located(texts, label["fault_lines"]) if answered else None,
        "named_affected": len(ablation.mentioned_numbers(texts) & affected),
        "citations": measured["citations"],
        "valid_citations": measured["valid_citations"],
        "specifics": measured.get("specifics", 0),
        "unsupported_specifics": measured.get("unsupported_specifics", []),
        "tool_calls": dict(sorted(outcomes.items())),
        "turns": measured.get("turns", 0),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_write_tokens": measured["cache_write_tokens"],
        "cache_read_tokens": measured["cache_read_tokens"],
        "cost_usd": usage["cost_usd"],
        "latency_ms": measured["latency_ms"],
        "texts": texts,
        "failure": investigation["failure"],
    }


def _majority(values: Sequence[bool | None]) -> bool | None:
    """A case's verdict over its repeats: true if more than half are true; None if none ran."""
    if not values:
        return None
    return sum(bool(v) for v in values) * 2 > len(values)


def scored(
    rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    cases = []
    for row in rows:
        label = labels[row["case_id"]]
        runs = [
            grade(run["investigation"], run["measures"], label) | {"rep": run["rep"]}
            for run in row["runs"]
            if "investigation" in run
        ]
        cases.append(
            {
                "case_id": row["case_id"],
                "kind": label["kind"],
                "family": label["family"],
                "variant": label["variant"],
                "origin": label["origin"],
                "fault_lines": label["fault_lines"],
                "failed": row.get("failed", []),
                "skipped": row["skipped"],
                "rules": grade(row["rules"], row["rules_measures"], label)
                if row["rules"]
                else None,
                "runs": runs,
                "not_run": [run for run in row["runs"] if "investigation" not in run],
                "model": {
                    "answered": _majority([r["answered"] for r in runs]),
                    "root_cause": _majority([r["root_cause"] for r in runs]),
                    "located": _majority([r["located"] for r in runs]),
                }
                if runs
                else None,
            }
        )
    return cases


def _rate(successes: int, trials: int) -> dict[str, Any]:
    interval = wilson(successes, trials)
    return {
        "n": successes,
        "of": trials,
        "wilson_95": [round(interval[0], 3), round(interval[1], 3)] if interval else None,
    }


def _arm(grades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    answered = [g for g in grades if g["answered"]]
    costs = [Decimal(g["cost_usd"]) for g in grades]
    stated = sum(g["specifics"] for g in grades)
    return {
        "investigations": len(grades),
        "statuses": dict(sorted(Counter(g["status"] for g in grades).items())),
        "answered": _rate(len(answered), len(grades)),
        "root_cause_of_all": _rate(sum(bool(g["root_cause"]) for g in grades), len(grades)),
        "root_cause_of_answered": _rate(
            sum(bool(g["root_cause"]) for g in answered), len(answered)
        ),
        "root_cause_first_of_answered": _rate(
            sum(bool(g["root_cause_first"]) for g in answered), len(answered)
        ),
        "located_of_answered": _rate(sum(bool(g["located"]) for g in answered), len(answered)),
        "citations": sum(g["citations"] for g in grades),
        "valid_citations": sum(g["valid_citations"] for g in grades),
        "specifics": stated,
        "unsupported_specifics": sum(len(g["unsupported_specifics"]) for g in grades),
        "tool_calls": dict(
            sorted(sum((Counter(g["tool_calls"]) for g in grades), Counter()).items())
        ),
        "cost_usd": str(sum(costs, Decimal(0))),
        "cost_usd_median": str(statistics.median(costs)) if costs else None,
        "cost_usd_max": str(max(costs)) if costs else None,
        "latency_ms_median": statistics.median([g["latency_ms"] for g in grades])
        if grades
        else None,
        "latency_ms_max": max((g["latency_ms"] for g in grades), default=None),
    }


def summarize(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    investigated = [c for c in cases if c["rules"] is not None]
    with_model = [c for c in investigated if c["model"] is not None]
    return {
        "cases": len(cases),
        "investigated": len(investigated),
        "skipped": dict(Counter(c["skipped"] for c in cases if c["skipped"])),
        "rules": _arm([c["rules"] for c in investigated]),
        "model": _arm([run for c in investigated for run in c["runs"]]),
        "by_case": {
            "cases_with_a_model_run": len(with_model),
            "model_answered": sum(bool(c["model"]["answered"]) for c in with_model),
            "model_named_root_cause": sum(bool(c["model"]["root_cause"]) for c in with_model),
            "rules_named_root_cause": sum(bool(c["rules"]["root_cause"]) for c in with_model),
            "model_only": sum(
                bool(c["model"]["root_cause"]) and not c["rules"]["root_cause"] for c in with_model
            ),
            "rules_only": sum(
                bool(c["rules"]["root_cause"]) and not c["model"]["root_cause"] for c in with_model
            ),
            "model_located": sum(bool(c["model"]["located"]) for c in with_model),
        },
    }


def results(
    rows: Sequence[Mapping[str, Any]],
    cases: EvalSet,
    *,
    live: bool,
    model: str,
    effort: str,
    reps: int,
    budget_usd: Decimal,
    budget: Budget = ablation.BUDGET,
) -> dict[str, Any]:
    from dataproof_investigator import PROMPT_VERSION, TOOL_VERSION

    graded = scored(rows, cases.labels())
    return {
        "evaluation": "ai-eval-v2: the Failure Investigator against the rules",
        "set": cases.name,
        "dry_run": not live,
        "model": model,
        "effort": effort,
        "reps": reps,
        "prompt_version": PROMPT_VERSION,
        "tool_version": TOOL_VERSION,
        "budget": {
            "approved_usd": str(budget_usd) if live else None,
            "case_cap_usd": str(budget.max_cost_usd),
            "max_tool_calls": budget.max_tool_calls,
            "max_turns": budget.max_turns,
        },
        "cases": graded,
        "summary": summarize(graded),
    }


def transcripts(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Each investigation's transcript, by case and repeat."""
    return {
        f"{row['case_id']}.rep{run['rep']}": run["transcript"]
        for row in rows
        for run in row["runs"]
        if "transcript" in run
    }


def not_started(document: Mapping[str, Any], reason: str) -> list[str]:
    """The investigations a stage did not start for ``reason`` (``STOPPED``, ``HALTED``, or
    the budget), as ``case rep``."""
    return [
        f"{case['case_id']} rep {run['rep']}"
        for case in document["cases"]
        for run in case["not_run"]
        if str(run.get("skipped", "")).startswith(reason)
    ]


def stopped(document: Mapping[str, Any]) -> bool:
    """Whether the stop rule ended the stage before every investigation ran."""
    return bool(not_started(document, STOPPED))


def health(kept: Mapping[str, Sequence[Mapping[str, Any]]], served_by: str | None) -> list[str]:
    """Faults in a stage's plumbing, from its transcripts; empty when there is none.

    A run that sent no request, a request that failed, and, for a live run (``served_by`` is
    the pinned model), a turn another model served. A stage stops at the first: the rest of
    its budget would buy nothing that could be reported. The first evaluation's gate checked
    spend and errors, and passed a stage in which no request was sent.
    """
    problems = []
    for key, turns in sorted(kept.items()):
        if not turns:
            problems.append(f"{key}: no request was sent")
        for index, turn in enumerate(turns, 1):
            if "error" in turn:
                problems.append(f"{key} turn {index}: {str(turn['error'])[:200]}")
            elif served_by is not None and turn.get("served_by") != served_by:
                problems.append(f"{key} turn {index}: served by {turn.get('served_by')}")
    return problems


def cache_misses(kept: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    """Turns that read nothing from the prompt cache although the turn before left a prefix
    in it. A prompt shorter than the model's minimum is never cached, and that is not a miss:
    only a prefix that was written or read, and then not read, is."""
    misses = []
    for key, turns in sorted(kept.items()):
        for index in range(1, len(turns)):
            before, turn = turns[index - 1], turns[index]
            if "error" in before or "error" in turn:
                continue
            cached = int(before.get("cache_write_tokens", 0)) + int(
                before.get("cache_read_tokens", 0)
            )
            if cached and not turn.get("cache_read_tokens"):
                misses.append(
                    f"{key} turn {index + 1}: nothing was read from the cache, where turn "
                    f"{index} left {cached} tokens"
                )
    return misses


def uncalled(document: Mapping[str, Any]) -> list[str]:
    """Model investigations that sent no turn: a run with any is not a measurement of them."""
    return [
        f"{case['case_id']} rep {run['rep']}"
        for case in document["cases"]
        for run in case["runs"]
        if run.get("turns", 0) == 0
    ]


def _shown(rate: Mapping[str, Any]) -> str:
    if rate["wilson_95"] is None:
        return "none"
    low, high = rate["wilson_95"]
    return f"{rate['n']} of {rate['of']} ({low:.0%} to {high:.0%})"


def report(document: Mapping[str, Any]) -> str:
    """The Markdown report of a v2 investigation run."""
    summary = document["summary"]
    by_case = summary["by_case"]
    lines = [
        f"# AI evaluation v2: the Failure Investigator on the {document['set']} set",
        "",
        "Generated by `python -m dataproof_benchmark eval2-report`. Do not edit by hand.",
        "",
    ]
    if document["dry_run"]:
        lines += ["**A dry run with a scripted model. These are not results.**", ""]
    if document["set"] == "dev":
        lines += ["**The development set: never a headline, whatever it shows.**", ""]
    budget = document["budget"]
    lines += [
        f"- Model `{document['model']}`, effort {document['effort']}; prompt "
        f"`{document['prompt_version']}`, tools `{document['tool_version']}`; "
        f"investigations a case: {document['reps']}.",
        f"- At most {budget['case_cap_usd']} USD, {budget['max_tool_calls']} tool calls and "
        f"{budget['max_turns']} turns an investigation; "
        f"{budget['approved_usd'] or 'no'} USD approved.",
        f"- {summary['investigated']} of {summary['cases']} cases had a failed check to explain.",
        "",
        "Rates are followed by their 95% Wilson interval. A cause or a location is graded only "
        "on an answer: an investigation that gave none is counted under Answered and nowhere "
        "else.",
        "",
        "| | Rules | Model |",
        "| --- | --- | --- |",
    ]
    for title, key in (
        ("Answered", "answered"),
        ("Root cause named, of all investigations", "root_cause_of_all"),
        ("Root cause named, of answers", "root_cause_of_answered"),
        ("Root cause in the first hypothesis, of answers", "root_cause_first_of_answered"),
        ("Faulty statement's lines stated, of answers", "located_of_answered"),
    ):
        lines.append(
            f"| {title} | {_shown(summary['rules'][key])} | {_shown(summary['model'][key])} |"
        )
    for title, pair in (
        ("Citations that exist / cited", ("valid_citations", "citations")),
        ("Unsupported specifics / stated", ("unsupported_specifics", "specifics")),
    ):
        cells = [f"{summary[arm][pair[0]]} / {summary[arm][pair[1]]}" for arm in ("rules", "model")]
        lines.append(f"| {title} | {cells[0]} | {cells[1]} |")
    model = summary["model"]
    lines += [
        f"| Cost, USD: total; median and maximum an investigation | 0 | {model['cost_usd']}; "
        f"{model['cost_usd_median']} and {model['cost_usd_max']} |",
        f"| Latency, ms: median and maximum | {summary['rules']['latency_ms_median']} and "
        f"{summary['rules']['latency_ms_max']} | {model['latency_ms_median']} and "
        f"{model['latency_ms_max']} |",
        "",
        f"By case, a case's verdict being the majority of its repeats: the model answered in "
        f"{by_case['model_answered']} of {by_case['cases_with_a_model_run']}; named the root "
        f"cause in {by_case['model_named_root_cause']} (the rules in "
        f"{by_case['rules_named_root_cause']}); only the model in {by_case['model_only']}, only "
        f"the rules in {by_case['rules_only']}; stated the faulty lines in "
        f"{by_case['model_located']}.",
        "",
        "## Cases",
        "",
    ]
    for case in document["cases"]:
        lines += [f"### `{case['case_id']}`", ""]
        if case["skipped"]:
            lines += [f"Not investigated: {case['skipped']}.", ""]
            continue
        rules = case["rules"]
        lines.append(f"- **Rules:** {rules['status']}; root cause {_word(rules['root_cause'])}.")
        lines += [f"  - {text}" for text in rules["texts"]]
        for run in case["runs"]:
            lines.append(
                f"- **Model, repeat {run['rep']}:** {run['status']}; root cause "
                f"{_word(run['root_cause'])}; lines {_word(run['located'])}; "
                f"{run['valid_citations']} of {run['citations']} citations exist; "
                f"{len(run['unsupported_specifics'])} of {run['specifics']} specifics "
                f"unsupported; {sum(run['tool_calls'].values())} tool calls in {run['turns']} "
                f"turns; {run['cost_usd']} USD; {run['latency_ms']} ms."
            )
            if run["failure"]:
                lines.append(f"  - No answer: {run['failure']}")
            lines += [f"  - {text}" for text in run["texts"]]
        lines += [f"- Repeat {run['rep']} not run: {run['skipped']}." for run in case["not_run"]]
        lines.append("")
    return "\n".join(lines)


def _word(value: bool | None) -> str:
    return {True: "yes", False: "no", None: "not graded"}[value]


# The Change Analyst ----------------------------------------------------------------------------


def dry_run_analyst(path: str) -> InvestigatorModel:
    """A scripted analyst with a real price: it reads the start of the change, then cites it."""
    final = (
        '{"hypotheses": [{"statement": "Scripted: the change was read.", '
        '"cited_evidence_ids": ["investigation.tool.1"], "confidence": "LOW"}], '
        '"suggested_action": null, "caveat": "A dry run: no model was called."}'
    )
    return ScriptedModel(
        [call("read_source", {"path": path, "start_line": 1, "end_line": 40}), answer(final)],
        name="scripted:dry-run",
        turn_ceiling_usd=ablation.DRY_RUN_CEILING_USD * ANALYST_MAX_TOKENS / ablation.MAX_TOKENS,
    )


def analyze(
    cases: EvalSet,
    model_for: Callable[[str], InvestigatorModel] | None,
    budget_usd: Decimal,
    *,
    reps: int = 1,
    case_ids: Sequence[str] | None = None,
    skip: Sequence[str] = (),
    journal: Journal | None = None,
    served_by: str | None = None,
) -> list[dict[str, Any]]:
    """Analyze every case's change before anything runs; labels are not read here.

    The stage stops, and keeps its records, as ``investigate`` does.
    """
    rows: list[dict[str, Any]] = []
    stage = _Stage(
        "analysis", budget_usd, ANALYST_CASE_CAP_USD, journal=journal, served_by=served_by
    )
    checked = False
    for case_id in cases.case_ids():
        if (case_ids and case_id not in case_ids) or case_id in skip:
            continue
        files = cases.changed(case_id)
        run_id = uuid.uuid4()
        rules = risk_rules(run_id, files, cases.migration_roots)
        row: dict[str, Any] = {"case_id": case_id, "rules": rules.model_dump(mode="json")}
        row["runs"] = []
        rows.append(row)
        if model_for is None:
            continue
        context = ToolContext(
            checks={},
            evidence={},
            source_archive=cases.archive(case_id),
            source_roots=cases.source_roots,
            databases={},
            changed_paths=tuple(files),
        )
        for rep in range(1, reps + 1):
            key = f"{case_id}.rep{rep}"
            recorder = Recorder(model_for(next(iter(files))))
            analyst = change_analyst(recorder, ANALYST_BUDGET)
            kept = stage.kept(key, rep, _interrupted(analyst, run_id))
            if kept is not None:
                row["runs"].append(kept)
                continue
            blocked = stage.blocked()
            if blocked is not None:
                row["runs"].append({"rep": rep, "skipped": blocked})
                continue
            if not checked:
                ablation.require_feasible_cap(analyst, context, ANALYST_CASE_CAP_USD)
                checked = True
            if journal is not None:
                recorder.on_turn = journal.start(key)
            analysis = analyst.investigate(run_id, context)
            run = {
                "rep": rep,
                "analysis": analysis.model_dump(mode="json"),
                "measures": measures(
                    analysis, recorder.tool_evidence(), recorder.final_text() or ""
                ),
                "transcript": recorder.turns,
            }
            if journal is not None:
                journal.finish(key, run)
            row["runs"].append(run)
            stage.count(key, run)
    return rows


def grade_analysis(
    analysis: Mapping[str, Any],
    label: Mapping[str, Any],
    *,
    turns: int | None,
    measured: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One analysis against its label. ``turns`` is None for the rules, which call no model.

    A model that sent no turn is ``called: False`` and nothing else is graded: in the first
    evaluation 34 such analyses were recorded as zero faults anticipated and zero false alarms.
    """
    raised = [
        h
        for h in analysis["hypotheses"]
        if turns is None or h["confidence"] in (Confidence.MEDIUM.value, Confidence.HIGH.value)
    ]
    texts = [h["statement"] for h in raised]
    called = turns is None or turns > 0
    # The rules always answer: no match is their answer that nothing was flagged.
    answered = called and (turns is None or analysis["status"] == "COMPLETED")
    fault = label["kind"] == "fault"
    graded: dict[str, Any] = {
        "status": analysis["status"],
        "called": called,
        "answered": answered,
        "risks": len(raised) if answered else None,
        "anticipated": rubric(label["rubric"], texts) if answered and fault else None,
        "located": located(texts, label["fault_lines"]) if answered and fault else None,
        "alarmed": bool(raised) if answered and not fault else None,
        "cost_usd": analysis["usage"]["cost_usd"],
        "latency_ms": analysis["usage"]["latency_ms"],
        "texts": texts,
        "failure": analysis["failure"],
    }
    if measured is not None:
        graded |= {
            "citations": measured["citations"],
            "valid_citations": measured["valid_citations"],
            "unsupported_specifics": measured.get("unsupported_specifics", []),
        }
    return graded


def analysis_results(
    rows: Sequence[Mapping[str, Any]],
    cases: EvalSet,
    *,
    live: bool,
    model: str | None,
    reps: int,
    budget_usd: Decimal | None,
) -> dict[str, Any]:
    labels = cases.labels()
    graded = []
    for row in rows:
        label = labels[row["case_id"]]
        runs = [
            grade_analysis(
                run["analysis"], label, turns=len(run["transcript"]), measured=run["measures"]
            )
            | {"rep": run["rep"]}
            for run in row["runs"]
            if "analysis" in run
        ]
        graded.append(
            {
                "case_id": row["case_id"],
                "kind": label["kind"],
                "family": label["family"],
                "variant": label["variant"],
                "rules": grade_analysis(row["rules"], label, turns=None, measured=None),
                "runs": runs,
                "not_run": [run for run in row["runs"] if "analysis" not in run],
            }
        )
    faults = [c for c in graded if c["kind"] == "fault"]
    clean = [c for c in graded if c["kind"] == "clean"]

    def arm(pick: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
        fault_runs = [g for c in faults for g in pick(c)]
        clean_runs = [g for c in clean for g in pick(c)]
        every = fault_runs + clean_runs
        answered_faults = [g for g in fault_runs if g["answered"]]
        answered_clean = [g for g in clean_runs if g["answered"]]
        return {
            "analyses": len(every),
            "not_called": sum(not g["called"] for g in every),
            "answered": _rate(sum(bool(g["answered"]) for g in every), len(every)),
            "faults_anticipated_of_answered": _rate(
                sum(bool(g["anticipated"]) for g in answered_faults), len(answered_faults)
            ),
            "clean_alarmed_of_answered": _rate(
                sum(bool(g["alarmed"]) for g in answered_clean), len(answered_clean)
            ),
            "cost_usd": str(sum((Decimal(g["cost_usd"]) for g in every), Decimal(0))),
        }

    return {
        "evaluation": "ai-eval-v2: the Change Analyst against the static risk rules",
        "set": cases.name,
        "dry_run": model is not None and not live,
        "model": model,
        "reps": reps,
        "budget": {
            "approved_usd": str(budget_usd) if live else None,
            "case_cap_usd": str(ANALYST_CASE_CAP_USD),
            "max_output_tokens": ANALYST_MAX_TOKENS,
        },
        "cases": graded,
        "summary": {
            "faults": len(faults),
            "clean": len(clean),
            "rules": arm(lambda c: [c["rules"]]),
            "model": arm(lambda c: c["runs"]),
        },
    }


def live_model(model: str, effort: str, max_tokens: int) -> InvestigatorModel:
    """A pinned model with no fallbacks, so that every turn has a cost ceiling."""
    import anthropic

    from dataproof_investigator.anthropic_model import PRICES, AnthropicModel

    if model not in PRICES:
        raise SystemExit(f"no price is recorded for {model}, so its spend could not be capped")
    return AnthropicModel(
        anthropic.Anthropic(), model=model, effort=effort, max_tokens=max_tokens, fallbacks=False
    )


def analysis_transcripts(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Each analysis's transcript, by case and repeat."""
    return {
        f"{row['case_id']}.rep{run['rep']}": run["transcript"]
        for row in rows
        for run in row["runs"]
        if "transcript" in run
    }
