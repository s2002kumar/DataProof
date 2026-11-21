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
* **Atomic graders.** Besides the keyword rubric: ``located``, whether every line range the
  answer states lies within the statement at fault, and ``statement_located``, whether the first
  range it states reaches that statement and no other. They are mechanical, not keywords, and
  neither decides: the owner's marks do.
* **Repeats, with intervals.** Every case can be investigated several times; rates carry
  Wilson intervals, and a case's verdict is the majority of its repeats.
* **A cap that cannot be met is an error before the run** (``ablation.require_feasible_cap``),
  and a model arm that sent no request is reported as not called, never as a score.

The harness uses only what the investigator package has offered since before ADR-0009, so the
same code runs the old investigator from a checkout of the old package: that is the old arm.
"""

import ast
import difflib
import hashlib
import io
import json
import math
import os
import platform
import re
import statistics
import subprocess
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
    ModelTurn,
    ScriptedModel,
    ToolContext,
    ToolResult,
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
LOCATED_MARGIN = 0
"""How far past the statement's own lines a stated range may reach. None: two lines reached
the neighbouring statement in two held-out cases."""
MAX_STATED_RANGES = 4
"""An answer that states more ranges than this has not located anything: it has listed the
file."""

INVESTIGATION_CAP_USD = Decimal("1.00")
"""The most one investigation may cost. An investigation of the first run cost 0.06 to 0.11
USD; a turn's worst case is about 0.25 USD, most of it output it never writes. The loop asks
for the answer when two worst-case turns no longer fit, so a cap of 0.50 USD could cut a
twelve-turn investigation off (a review showed it with the first run's token counts)."""
INVESTIGATION_START_USD = Decimal("0.60")
"""An investigation is started only while the stage has this much left, and is given what is
left as its cap if that is less than the cap above. Below this it would be asked for its
answer after a turn or two, and would measure the budget, not the investigator."""
BUDGET = Budget(max_tool_calls=12, max_turns=16, max_cost_usd=INVESTIGATION_CAP_USD)

ANALYST_MAX_TOKENS = 4000
"""An analysis writes one answer of a few risks; its turns are short. With 4,000 output tokens
a worst-case turn costs about 0.11 USD, so the cap below leaves room for several."""
ANALYST_CASE_CAP_USD = Decimal("0.40")
"""The Change Analyst's cap for one analysis: a briefing, a read or two and an answer cost a
few cents, and two worst-case turns must fit beside them. The first evaluation's cap, 0.15 USD
against a 0.194 USD turn at 8,000 output tokens, admitted no turn at all."""
ANALYSIS_START_USD = Decimal("0.30")
ANALYST_BUDGET = Budget(max_tool_calls=8, max_turns=10, max_cost_usd=ANALYST_CASE_CAP_USD)

ModelFactory = Callable[[], InvestigatorModel]
STOPPED = "the stop rule: too many investigations gave no answer"
FROZEN = (
    "benchmarks/src",
    "benchmarks/heldout",
    "benchmarks/launcher",
    "packages",
    "examples/subscriptions",
    "sample-app",
    "docs/AI_EVALUATION_V2.md",
)
"""What a stage's outcome depends on: a results file says whether any of it differed from the
commit, and the launcher refuses a paid stage if it does. Results kept elsewhere in the
repository are not part of it."""
PROVIDER_SIDE = frozenset({401, 402, 403, 404, 408, 409, 429, 500, 502, 503, 504, 529})
"""Statuses that say nothing about the investigation: the key, the account, the model's
availability, the provider's load. An error the SDK raised with no status (a timeout, a lost
connection) is the provider's side too."""


class ProviderStopped(Exception):  # noqa: N818 - an outcome the owner reads, not a program error
    """The provider failed an investigation's request after every resend. The stage stops at
    once, and the investigation's record is left open: taking the stage up again starts that
    investigation again, charged what it had already spent."""

    def __init__(self, key: str, error: str) -> None:
        super().__init__(f"{key}: the provider failed a request after every resend: {error}")


def provider_side(turn: Mapping[str, Any]) -> bool:
    """Whether a failed request failed on the provider's side, not the investigation's."""
    if "error" not in turn:
        return False
    status = turn.get("status_code")
    if status is None:
        return bool(turn.get("transport"))
    return status in PROVIDER_SIDE or (status == 400 and "credit balance" in str(turn["error"]))


def _stopped_by_provider(key: str, turns: Sequence[Mapping[str, Any]]) -> None:
    if turns and provider_side(turns[-1]):
        raise ProviderStopped(key, str(turns[-1]["error"])[:300])


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
    stands, unanswered, and never sent again, unless the provider stopped it (``_Stage.kept``).
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

    def retire(self, key: str) -> list[dict[str, Any]]:
        """Set aside the record of an investigation that never ended, so that it can be started
        again; its turns stay in the journal under another name, and are charged."""
        turns = self.interrupted(key) or []
        number = 1 + len(list(self.folder.glob(f"{key}.retired-*.jsonl")))
        self._turns(key).rename(self.folder / f"{key}.retired-{number}.jsonl")
        return turns

    def retired_usd(self, key: str) -> Decimal:
        """What the attempts of an investigation set aside after provider errors spent."""
        total = Decimal(0)
        for path in sorted(self.folder.glob(f"{key}.retired-*.jsonl")):
            for line in path.read_text("utf-8").splitlines()[1:]:
                try:
                    turn = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += Decimal(str(turn.get("cost_usd", "0"))) + Decimal(
                    str(turn.get("unconfirmed_usd", "0"))
                )
        return total

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
    start: Decimal
    """What the stage must have left for another investigation to start."""
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
        if self.budget_usd - self.spent < self.start:
            return "the budget left cannot cover it"
        if self.stop_after_unanswered is not None and self.unanswered >= self.stop_after_unanswered:
            return STOPPED
        return None

    def limited(self, budget: Budget) -> Budget:
        """The budget of the next investigation: never more than the stage has left, so the
        stage's budget is a limit no investigation can take it past."""
        return replace(budget, max_cost_usd=min(self.cap, self.budget_usd - self.spent))

    def kept(self, key: str, rep: int, interrupted: Investigation) -> dict[str, Any] | None:
        """The record of an investigation that a run of this stage already finished, which this
        one continues: it is counted, and nothing is sent. None if it is to be run.

        An investigation the provider stopped (its last record is a request the provider
        failed) is started again: that says nothing about the investigation. Any other that
        started and never ended (the process died, or was stopped) is counted as it stands,
        unanswered: starting it again would give a second draw to an investigation that may
        have been stopped for what it was doing. What every earlier attempt spent is charged
        each time the stage is taken up, as the journal holds it.
        """
        if self.journal is None:
            return None
        run = self.journal.finished(key)
        if run is None:
            turns = self.journal.interrupted(key)
            if turns is not None and turns and provider_side(turns[-1]):
                self.journal.retire(key)
                say(f"{key}: started again after a provider error; what it spent is charged")
                turns = None
            if turns is not None:
                investigation = _with_usage(interrupted, turns)
                run = {
                    "rep": rep,
                    self.record: investigation.model_dump(mode="json"),
                    "measures": measures(investigation, {}, "") | _turn_measures(turns),
                    "transcript": turns,
                    "interrupted": True,
                }
                self.journal.finish(key, run)
        self.spent += self.journal.retired_usd(key)
        if run is None:
            return None
        self.count(key, run, check=False)
        return run

    def count(self, key: str, run: Mapping[str, Any], *, check: bool = True) -> None:
        """Count a finished run: its cost, whether it answered, and whether it shows a fault in
        the plumbing that must stop the stage (not for a run kept from before: taking the
        stage up again was a decision made knowing of it)."""
        record = run[self.record]
        self.counted += 1
        # A request that got no answer may have been billed: it counts at its worst case.
        self.spent += Decimal(record["usage"]["cost_usd"]) + unconfirmed(run["transcript"])
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


def unconfirmed(turns: Sequence[Mapping[str, Any]]) -> Decimal:
    """What requests that got no answer could have cost, had the provider processed them."""
    return sum((Decimal(str(t.get("unconfirmed_usd", "0"))) for t in turns), Decimal(0))


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
    """A Python source file without what says, in words, what it does: its comments, every
    statement that is only a string (docstrings among them), and the body of ``downgrade``.
    Every other line is unchanged.

    A revision's comments say what it does wrong ("integer / integer is integer division"), and
    an investigator that reads the answer has explained nothing. A ``downgrade`` is not what
    ran, and it can say the same in a string (a review found "the original column order is
    not restored" in one). Statements keep their text, so a line range in the blinded file
    still names a statement.
    """
    tree = ast.parse(source)
    dropped: set[int] = set()
    replaced: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            first, last = node.body[0], node.body[-1]
            dropped.update(range(first.lineno, (last.end_lineno or last.lineno) + 1))
            replaced[first.lineno] = " " * first.col_offset + "raise NotImplementedError"
            continue
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        strings = [
            statement
            for statement in node.body
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ]
        if len(strings) == len(node.body):
            strings = strings[:-1]  # a body must keep a statement
        for statement in strings:
            dropped.update(range(statement.lineno, (statement.end_lineno or statement.lineno) + 1))
    comments = {
        token.start[0]: token.start[1]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
    kept = []
    for number, line in enumerate(source.splitlines(), 1):
        if number in replaced:
            kept.append(replaced[number])
        if number in dropped:
            continue
        if number in comments:
            line = line[: comments[number]].rstrip()
            if not line:
                continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n\n", "\n".join(kept).strip("\n") + "\n")


def marker_spans(source: str, markers: Sequence[str]) -> list[tuple[int, int]] | None:
    """The lines of the smallest statement that holds each marker; None if one holds none."""
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
    return spans


def statement_lines(source: str, markers: Sequence[str]) -> tuple[int, int] | None:
    """The lines of the statements that hold the markers: the smallest statement for each."""
    spans = marker_spans(source, markers)
    return (min(s for s, _ in spans), max(e for _, e in spans)) if spans else None


def differing_lines(safe: str, faulty: str) -> list[int]:
    """Each line of ``faulty`` that differs from ``safe``; a deletion marks the line after it."""
    matcher = difflib.SequenceMatcher(a=safe.splitlines(), b=faulty.splitlines(), autojunk=False)
    return [
        line
        for tag, _, _, start, end in matcher.get_opcodes()
        if tag != "equal"
        for line in range(start + 1, max(end, start + 1) + 1)
    ]


def changed_lines(safe: str, faulty: str) -> tuple[int, int] | None:
    """The lines of ``faulty`` that differ from ``safe``: where a generated fault is."""
    differing = differing_lines(safe, faulty)
    return (min(differing), max(differing)) if differing else None


def simple_statements(source: str) -> list[tuple[int, int]] | None:
    """The lines of each simple statement of a Python file (a call such as ``op.execute(...)``,
    an assignment, a return), in order. A definition or a block is not one: its own simple
    statements are listed. None if the file does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return sorted(
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and "body" not in node._fields and "cases" not in node._fields
    )


def at_fault(
    statements: Sequence[tuple[int, int]] | None, lines: Sequence[int]
) -> list[tuple[int, int]] | None:
    """The simple statements that hold any of ``lines``: the statements at fault."""
    if statements is None:
        return None
    return [(s, e) for s, e in statements if any(s <= line <= e for line in lines)] or None


_RANGE = re.compile(
    r"(?:\blines?\s+|\bL|\.py:)(\d{1,4})(?:\s*(?:-|\N{EN DASH}|to|through)\s*L?(\d{1,4}))?",
    re.IGNORECASE,
)
_FILE = re.compile(r"[\w./-]*\w\.py\b")
_NEAR = 80
_AFTER = re.compile(r"\s*\)?\s*(?:of|in)\s+(?:the\s+file\s+)?`?([\w./-]*\w\.py)\b", re.IGNORECASE)
"""How far from a stated range a file name is taken to be the file it speaks of."""


def stated_ranges(text: str, path: str | None = None) -> list[tuple[int, int]]:
    """Line ranges a text states: "lines 22-30", "line 25", "L22-L30", "0002_change.py:25".

    With ``path``, a range stated of another file is left out: one whose nearest file name
    before it, in the same sentence, is not ``path``'s. "Line 20 and 187 invoices" is one line,
    not a range.
    """
    ranges = []
    name = path.rsplit("/", 1)[-1] if path else None
    for found in _RANGE.finditer(text):
        start = int(found.group(1))
        end = int(found.group(2)) if found.group(2) else start
        if name is not None:
            # "line 40 of app/models.py" names its file after the range; "in 0002_change.py,
            # lines 18-20" before it.
            after = _AFTER.match(text, found.end())
            if after is not None:
                attributed: str | None = after.group(1).rsplit("/", 1)[-1]
            else:
                before = text[max(0, found.start() - _NEAR) : found.start()]
                before = re.split(r"[.;!?]\s", before)[-1]
                named_files = [match.rsplit("/", 1)[-1] for match in _FILE.findall(before)]
                attributed = named_files[-1] if named_files else None
            if attributed is not None and attributed != name:
                continue
        ranges.append((min(start, end), max(start, end)))
    return ranges


def located(
    texts: Sequence[str], fault: Sequence[int] | None, path: str | None = None
) -> bool | None:
    """Whether the texts point at the statement at fault: they state at least one line range
    of the changed file, at most ``MAX_STATED_RANGES``, and every one lies within the
    statement's lines.

    A range that merely overlaps the statement does not count (a constant "lines 10-29"
    overlapped every fault of the held-out set), nor does an answer that also names another
    statement ("see line 10 and line 26" passed eleven of eleven when one range sufficed). It
    is a mechanical proxy: the owner's mark on each answer decides A4.
    """
    if fault is None:
        return None
    low, high = fault[0] - LOCATED_MARGIN, fault[1] + LOCATED_MARGIN
    ranges = {span for text in texts for span in stated_ranges(text, path)}
    if not ranges or len(ranges) > MAX_STATED_RANGES:
        return False
    inside = [low <= start and end <= high for start, end in ranges]
    # A range that holds the whole statement is context ("in upgrade(), lines 10-30"); one
    # that misses it, or cuts across its edge, points elsewhere.
    around = [start <= low and high <= end for start, end in ranges]
    return any(inside) and all(i or a for i, a in zip(inside, around, strict=True))


def statement_located(texts: Sequence[str], label: Mapping[str, Any]) -> bool | None:
    """Whether the first line range an answer states of the changed file points at the
    statement at fault: it reaches a statement at fault and no other statement of the file.

    The investigator's proxy for the faulty statement, reported beside ``located``, which the
    development stage showed rejects a correct answer for naming its context: of 18 correct
    answers it accepted 4, each of the others having also named the INSERT the fault came
    after, or the table's definition, or a line just past the diff. Only the first range
    counts, so naming the context later does not matter, and listing ranges does not help: a
    constant range is right only where it happens to be. Neither proxy decides A4 (the owner's
    marks do), and ``located`` alone grades the Change Analyst, as the protocol says.
    """
    faulty, statements = label.get("fault_statements"), label.get("statements")
    if not faulty or statements is None:
        return None
    wanted = {(int(s), int(e)) for s, e in faulty}
    for text in texts:
        ranges = stated_ranges(text, label.get("path"))
        if ranges:
            start, end = ranges[0]
            reached = {(int(s), int(e)) for s, e in statements if s <= end and start <= e}
            return bool(reached) and reached <= wanted
    return False


def named(groups: Sequence[Sequence[str]] | None, texts: Sequence[str]) -> bool | None:
    """The keyword proxy for naming a cause: every group of terms must have a term that
    starts a word of the texts ("round" matches "rounded", "cast" does not match "forecast").

    A proxy, and a weak one both ways: a hedge that lists every cause matches (``hedged``
    catches that), and a correct explanation in other words does not. It is reported beside
    the owner's blinded marks, which decide.
    """
    if groups is None:
        return None
    joined = " ".join(texts).lower()
    return all(
        any(re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}", joined) for term in group)
        for group in groups
    )


def hedged(
    texts: Sequence[str],
    others: Sequence[Sequence[Sequence[str]]],
    own: Sequence[Sequence[str]] | None = None,
) -> bool:
    """Whether the texts also match two or more of the other cases' rubrics: an answer that
    lists every cause has named none.

    A term the case's own rubric has is no sign of another cause, and is not counted for one:
    a lost and a duplicated row are "missing" and "duplicated", which are also a partial
    migration's and a retry's terms, so every correct answer to such a case was counted a
    hedge (both of the development stage's). A rubric left with an empty group cannot match.
    """
    shared = {term for group in own or () for term in group}
    counted = 0
    for groups in others:
        kept = [[term for term in group if term not in shared] for group in groups]
        if all(kept) and named(kept, texts):
            counted += 1
    return counted >= 2


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
        for relative, text in rendered(list(self._cases.values()), [], seed).items():
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
            fault = case.label.kind == "fault"
            statements = simple_statements(case.source())
            found[case_id] = {
                "kind": case.label.kind,
                "family": case.label.family,
                "variant": case.label.variant,
                "origin": "generated",
                "rubric": ablation.RUBRIC.get(case.label.family),
                "fault_lines": changed_lines(safe, case.source()) if fault else None,
                "statements": statements,
                "fault_statements": at_fault(statements, differing_lines(safe, case.source()))
                if fault
                else None,
                "path": next(iter(self.changed(case_id))),
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
            source = self._revision(case_id)
            statements = simple_statements(source)
            spans = marker_spans(source, markers) if markers else None
            found[case_id] = {
                "kind": label["kind"],
                "family": label["family"],
                "variant": label["change"],
                "origin": label["origin"],
                "rubric": label.get("rubric"),
                "fault_lines": statement_lines(source, markers) if markers else None,
                "statements": statements,
                "fault_statements": at_fault(
                    statements, [n for s, e in spans for n in range(s, e + 1)]
                )
                if spans
                else None,
                "path": HELDOUT_REVISION,
                "affected": [],
            }
        return found


def references(cases: EvalSet) -> dict[str, dict[str, Any]]:
    """What the owner reads an answer against: each fault's family, where its pattern comes
    from, and the faulty statement as the model's file has it, with its lines."""
    found = {}
    for case_id, label in cases.labels().items():
        if label["kind"] != "fault" or label["fault_lines"] is None:
            continue
        source = next(iter(cases.changed(case_id).values())).splitlines()
        low, high = label["fault_lines"]
        found[case_id] = {
            "family": label["family"],
            "origin": label["origin"],
            "fault_lines": [low, high],
            "statement": source[low - 1 : high],
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
    """A scripted investigator with a real price. It does what a live one is asked to: the
    briefing, then the first changed file it names, then an answer citing both. So a dry run
    exercises the briefing and a source read on every real case, at no cost.

    It reports the least a live turn's ceiling can be, so that a cap no live turn could meet
    stops a dry run too. The first evaluation's dry run had no price and could not see one.
    """
    return _BriefedModel()


def _changed_path(results: Sequence[ToolResult]) -> str | None:
    """The first changed file a briefing result names, if the results hold one."""
    for result in results:
        if result.is_error:
            continue
        data = json.loads(result.content).get("data", {})
        changed = data.get("changed_files") if isinstance(data, dict) else None
        if changed:
            return str(changed[0]["path"])
    return None


class _BriefedModel:
    """Scripted: the briefing, the changed file, an answer. An investigator without a
    briefing (the old arm) reads the first failed check instead."""

    name = "scripted:dry-run"

    def open(self, *, system: str, task: str, tools: Any, output_schema: Any) -> Any:
        names = {tool.name for tool in tools}
        found = re.search(r"^- (\S+): (?:FAIL|ERROR)", task, re.MULTILINE)
        check = found.group(1) if found else "none"

        def second(results: Sequence[ToolResult]) -> ModelTurn:
            path = _changed_path(results)
            if path is None:
                return call("get_check_result", {"check_id": check}, "t2")
            return call("read_source", {"path": path, "start_line": 1, "end_line": 200}, "t2")

        cited = ["investigation.tool.1", "investigation.tool.2"]
        final = {
            "hypotheses": [
                {
                    "statement": "Scripted: the briefing and the changed file were read.",
                    "cited_evidence_ids": cited,
                    "confidence": "LOW",
                }
            ],
            "suggested_action": None,
            "caveat": "A dry run: no model was called.",
        }
        first = (
            call("get_briefing", {}, "t1")
            if "get_briefing" in names
            else call("get_check_result", {"check_id": check}, "t1")
        )
        model = ScriptedModel(
            [first, second, answer(json.dumps(final))],
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
    budget: Budget = BUDGET,
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
    unknown = sorted(set(case_ids or ()) - set(cases.case_ids()))
    if unknown:
        raise SystemExit(f"no such case in the {cases.name} set: {', '.join(unknown)}")
    cap = budget.max_cost_usd or INVESTIGATION_CAP_USD
    stage = _Stage(
        "investigation",
        budget_usd,
        cap,
        min(cap, INVESTIGATION_START_USD),
        journal=journal,
        served_by=served_by,
        stop_after_unanswered=stop_after_unanswered,
    )
    checked = False
    with reader(admin_dsn) as as_reader, databases(admin_dsn) as new_database:
        reference_dsn = cases.reference(new_database)
        staged: list[tuple[dict[str, Any], ToolContext, uuid.UUID]] = []
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
            row["_checks"], row["_evidence"] = checks, evidence
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
            staged.append((row, context, run_id))
        # In rounds: every case once, then every case again. If a stage ends early, by its
        # budget or its stop rule, what is missing is a round, the same for every case, and
        # not the last cases of the set.
        for rep in range(1, reps + 1):
            for row, context, run_id in staged:
                key = f"{row['case_id']}.rep{rep}"
                recorder = Recorder(model_for())
                investigator = Investigator(recorder, stage.limited(budget))
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
                _stopped_by_provider(key, recorder.turns)
                texts = evidence_texts(row["_checks"], row["_evidence"], recorder.tool_evidence())
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
    for row in rows:
        row.pop("_checks", None)
        row.pop("_evidence", None)
    return rows


def _texts(investigation: Mapping[str, Any]) -> list[str]:
    texts = [h["statement"] for h in investigation["hypotheses"]]
    action = investigation.get("suggested_action")
    return texts + ([action] if action else [])


def other_rubrics(
    labels: Mapping[str, Mapping[str, Any]], case_id: str
) -> list[Sequence[Sequence[str]]]:
    """The distinct rubrics of the set's other cases, for ``hedged``."""
    own = labels[case_id].get("rubric")
    seen: list[Sequence[Sequence[str]]] = []
    for other, label in sorted(labels.items()):
        groups = label.get("rubric")
        if other != case_id and groups and groups != own and groups not in seen:
            seen.append(groups)
    return seen


def grade(
    investigation: Mapping[str, Any],
    measured: Mapping[str, Any],
    label: Mapping[str, Any],
    others: Sequence[Sequence[Sequence[str]]] = (),
) -> dict[str, Any]:
    """One investigation against its label, by the mechanical graders.

    Causes and locations are graded on answers only: an investigation that gave none is
    ``answered: False``, and nothing more is claimed. They are graded where an answer commits
    itself: the cause in the first hypothesis, the statement in the first hypothesis or the
    suggested action. Ten hypotheses of 2,000 characters are room to mention everything.
    """
    texts = _texts(investigation)
    answered = investigation["status"] == "COMPLETED"
    first = [h["statement"] for h in investigation["hypotheses"][:1]]
    action = investigation.get("suggested_action")
    pointed = first + ([action] if action else [])
    usage = investigation["usage"]
    outcomes = Counter(c["outcome"] for c in investigation["tool_calls"])
    affected = {int(n) for n in label.get("affected") or []}
    gradable = answered and label["rubric"] is not None
    hedge = hedged(first, others, label["rubric"]) if gradable else None
    return {
        "status": investigation["status"],
        "answered": answered,
        "root_cause": bool(named(label["rubric"], first)) and not hedge if gradable else None,
        "hedged": hedge,
        "located": located(pointed, label["fault_lines"], label.get("path")) if answered else None,
        "statement_located": statement_located(pointed, label) if answered else None,
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
        # The whole answer, as it was given: confidence, citations and caveat included.
        "answer": {
            "hypotheses": investigation["hypotheses"],
            "suggested_action": action,
            "caveat": investigation.get("caveat"),
        }
        if answered
        else None,
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
        others = other_rubrics(labels, row["case_id"])
        runs = [
            grade(run["investigation"], run["measures"], label, others) | {"rep": run["rep"]}
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
                "rules": grade(row["rules"], row["rules_measures"], label, others)
                if row["rules"]
                else None,
                "runs": runs,
                "not_run": [run for run in row["runs"] if "investigation" not in run],
                "model": {
                    "answered": _majority([r["answered"] for r in runs]),
                    "root_cause": _majority([r["root_cause"] for r in runs]),
                    "located": _majority([r["located"] for r in runs]),
                    "statement_located": _majority([r.get("statement_located") for r in runs]),
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
    # Over answers only: a rejected answer is never shown, so what it stated is not counted.
    stated = sum(g["specifics"] for g in answered)
    # What a case's label cannot grade (no rubric, no faulty statement) is in no denominator.
    with_rubric = [g for g in answered if g["root_cause"] is not None]
    with_lines = [g for g in answered if g["located"] is not None]
    with_statement = [g for g in answered if g.get("statement_located") is not None]
    return {
        "investigations": len(grades),
        "statuses": dict(sorted(Counter(g["status"] for g in grades).items())),
        "answered": _rate(len(answered), len(grades)),
        "root_cause_of_all": _rate(sum(bool(g["root_cause"]) for g in grades), len(grades)),
        "root_cause_of_answered": _rate(
            sum(bool(g["root_cause"]) for g in with_rubric), len(with_rubric)
        ),
        "hedged_of_answered": _rate(sum(bool(g["hedged"]) for g in with_rubric), len(with_rubric)),
        "located_of_answered": _rate(sum(bool(g["located"]) for g in with_lines), len(with_lines)),
        "statement_located_of_answered": _rate(
            sum(bool(g["statement_located"]) for g in with_statement), len(with_statement)
        ),
        "citations": sum(g["citations"] for g in grades),
        "valid_citations": sum(g["valid_citations"] for g in grades),
        "specifics": stated,
        "unsupported_specifics": sum(len(g["unsupported_specifics"]) for g in answered),
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


def summarize(cases: Sequence[Mapping[str, Any]], reps: int = 1) -> dict[str, Any]:
    investigated = [c for c in cases if c["rules"] is not None]
    with_model = [c for c in investigated if c["model"] is not None]
    # A case with a rubric, whether or not the rules answered it: rules that gave no answer
    # did not name the cause, as A3 counts it from the owner's marks. (A case the rules could
    # not answer was left out until the development stage showed one, dev-111.)
    gradable = [
        c
        for c in with_model
        if c["rules"]["root_cause"] is not None
        or any(run["root_cause"] is not None for run in c["runs"])
    ]
    runs = [run for c in investigated for run in c["runs"]]
    return {
        "cases": len(cases),
        "investigated": len(investigated),
        # Every failing case, every repeat: what a rate is "of", whether or not it ran.
        "investigations_expected": len(investigated) * reps,
        "investigations_run": len(runs),
        "skipped": dict(Counter(c["skipped"] for c in cases if c["skipped"])),
        "rules": _arm([c["rules"] for c in investigated]),
        "model": _arm(runs),
        "by_case": {
            "cases_with_a_model_run": len(with_model),
            "cases_with_a_rubric": len(gradable),
            "model_answered": sum(bool(c["model"]["answered"]) for c in with_model),
            "model_named_root_cause": sum(bool(c["model"]["root_cause"]) for c in gradable),
            "rules_named_root_cause": sum(bool(c["rules"]["root_cause"]) for c in gradable),
            "model_only": sum(
                bool(c["model"]["root_cause"]) and not c["rules"]["root_cause"] for c in gradable
            ),
            "rules_only": sum(
                bool(c["rules"]["root_cause"]) and not c["model"]["root_cause"] for c in gradable
            ),
            "model_located": sum(bool(c["model"]["located"]) for c in with_model),
            "model_statement_located": sum(
                bool(c["model"].get("statement_located")) for c in with_model
            ),
        },
    }


def _git(folder: Path, *arguments: str) -> str | None:
    try:
        done = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            ["git", "-C", str(folder), *arguments],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            env={k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")},
        )
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def provenance(cases: EvalSet) -> dict[str, Any]:
    """Which code produced a results file. The first evaluation's files did not say, and a
    review found that this machine's environment could run another checkout's code than the
    one a command was started in. No local path is recorded: a results file is published."""
    import dataproof_investigator

    here = Path(__file__).resolve().parent
    package = Path(dataproof_investigator.__file__).resolve().parent
    labels = HELDOUT_LABELS if cases.name == "heldout" else None
    try:
        from importlib.metadata import version

        sdk: str | None = version("anthropic")
    except Exception:
        sdk = None
    frozen = FROZEN
    return {
        "harness_commit": _git(here, "rev-parse", "HEAD"),
        "harness_modified": bool(_git(harness.REPOSITORY, "status", "--porcelain", "--", *frozen)),
        "investigator_commit": _git(package, "rev-parse", "HEAD"),
        "investigator_in_this_checkout": package.is_relative_to(harness.REPOSITORY),
        "labels_sha256": hashlib.sha256(labels.read_bytes()).hexdigest() if labels else None,
        "dev_seed": DEV_SEED if cases.name == "dev" else None,
        "max_output_tokens": ablation.MAX_TOKENS,
        "python": platform.python_version(),
        "anthropic_sdk": sdk,
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
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
    budget: Budget = BUDGET,
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
            "start_usd": str(INVESTIGATION_START_USD),
            "max_tool_calls": budget.max_tool_calls,
            "max_turns": budget.max_turns,
        },
        "provenance": provenance(cases),
        "cases": graded,
        "summary": summarize(graded, reps),
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
            # A database this harness staged did not answer a tool: what the model then says
            # is about the harness, not about the migration.
            if any(
                phrase in str(result.get("content", ""))
                for result in turn.get("tool_results", [])
                for phrase in ("could not be asked just now",)
            ):
                problems.append(f"{key} turn {index}: a tool could not reach its database")
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


def _shown(rate: Mapping[str, Any] | None) -> str:
    # None: a measure the run's code did not have (a stage run before it was added).
    if rate is None:
        return "not recorded"
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
        f"- {summary['investigations_run']} of {summary['investigations_expected']} "
        "investigations ran (every failing case, every repeat).",
        "",
        "Rates are followed by their 95% Wilson interval. A cause or a location is graded only "
        "on an answer: an investigation that gave none is counted under Answered and nowhere "
        "else. **These are the mechanical graders.** The cause is a keyword proxy on the "
        "first hypothesis; it misses a correct explanation in other words, and the owner's "
        "blinded marks decide (`eval2-verdict`). The location is graded two ways: every line "
        "range stated lies within the faulty statement's lines (which fails an answer that "
        "also names its context), and the first range stated reaches the faulty statement "
        "and no other. The owner's marks decide that too.",
        "",
        "| | Rules | Model |",
        "| --- | --- | --- |",
    ]
    for title, key in (
        ("Answered", "answered"),
        ("Root cause named, of all investigations", "root_cause_of_all"),
        ("Root cause named in the first hypothesis, of answers", "root_cause_of_answered"),
        ("First hypothesis hedged over several causes, of answers", "hedged_of_answered"),
        ("Faulty statement's lines stated, of answers", "located_of_answered"),
        (
            "First range stated points at the faulty statement, of answers",
            "statement_located_of_answered",
        ),
    ):
        lines.append(
            f"| {title} | {_shown(summary['rules'].get(key))} | "
            f"{_shown(summary['model'].get(key))} |"
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
        f"{by_case['model_located']}; pointed first at the faulty statement in "
        f"{by_case.get('model_statement_located', 'none recorded')}.",
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
                f"{_word(run['root_cause'])}; lines {_word(run['located'])}; statement "
                f"{_word(run.get('statement_located'))}; "
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
    """A scripted analyst with a real price: the briefing, the start of the change, an answer
    citing the read."""
    final = (
        '{"hypotheses": [{"statement": "Scripted: the change was read.", '
        '"cited_evidence_ids": ["investigation.tool.2"], "confidence": "LOW"}], '
        '"suggested_action": null, "caveat": "A dry run: no model was called."}'
    )
    return ScriptedModel(
        [
            call("get_briefing", {}, "t1"),
            call("read_source", {"path": path, "start_line": 1, "end_line": 40}, "t2"),
            answer(final),
        ],
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
    stop_after_unanswered: int | None = None,
) -> list[dict[str, Any]]:
    """Analyze every case's change before anything runs; labels are not read here.

    The stage runs in rounds, stops, and keeps its records, as ``investigate`` does.
    """
    rows: list[dict[str, Any]] = []
    unknown = sorted(set(case_ids or ()) - set(cases.case_ids()))
    if unknown:
        raise SystemExit(f"no such case in the {cases.name} set: {', '.join(unknown)}")
    stage = _Stage(
        "analysis",
        budget_usd,
        ANALYST_CASE_CAP_USD,
        ANALYSIS_START_USD,
        journal=journal,
        served_by=served_by,
        stop_after_unanswered=stop_after_unanswered,
    )
    checked = False
    staged: list[tuple[dict[str, Any], ToolContext, uuid.UUID, str]] = []
    for case_id in cases.case_ids():
        if (case_ids and case_id not in case_ids) or case_id in skip:
            continue
        files = cases.changed(case_id)
        run_id = uuid.uuid4()
        rules = risk_rules(run_id, files, cases.migration_roots)
        row: dict[str, Any] = {"case_id": case_id, "rules": rules.model_dump(mode="json")}
        row["runs"] = []
        rows.append(row)
        context = ToolContext(
            checks={},
            evidence={},
            source_archive=cases.archive(case_id),
            source_roots=cases.source_roots,
            databases={},
            changed_paths=tuple(files),
        )
        staged.append((row, context, run_id, next(iter(files))))
    if model_for is None:
        return rows
    for rep in range(1, reps + 1):  # in rounds, as ``investigate``
        for row, context, run_id, first_file in staged:
            key = f"{row['case_id']}.rep{rep}"
            recorder = Recorder(model_for(first_file))
            analyst = change_analyst(recorder, stage.limited(ANALYST_BUDGET))
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
            _stopped_by_provider(key, recorder.turns)
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
    """One analysis against its label. ``turns`` is None for the rules, which call no model;
    for a model it counts the turns that were answered.

    A model that sent no turn is ``called: False`` and nothing else is graded: in the first
    evaluation 34 such analyses were recorded as zero faults anticipated and zero false alarms.

    What is graded is a risk raised at MEDIUM or HIGH confidence. On a fault, ``flagged``:
    one of them points at the faulty statement's lines. That is the measure both arms are
    held to, because a rule states where it matched in fixed words and can never use a
    rubric's vocabulary; ``anticipated`` is the keyword proxy, reported beside it. On a clean
    control, ``alarmed``: any such risk at all.
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
    lines = label["fault_lines"]
    graded: dict[str, Any] = {
        "status": analysis["status"],
        "called": called,
        "answered": answered,
        "risks": len(raised) if answered else None,
        "flagged": any(bool(located([text], lines, label.get("path"))) for text in texts)
        if answered and fault and lines
        else None,
        "anticipated": named(label["rubric"], texts)
        if answered and fault and label["rubric"]
        else None,
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
            "answer": {
                "hypotheses": analysis["hypotheses"],
                "suggested_action": analysis.get("suggested_action"),
                "caveat": analysis.get("caveat"),
            }
            if answered
            else None,
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
    effort: str | None = None,
) -> dict[str, Any]:
    from dataproof_investigator import TOOL_VERSION
    from dataproof_investigator.analyst import ANALYST_PROMPT_VERSION

    labels = cases.labels()
    graded = []
    for row in rows:
        label = labels[row["case_id"]]
        runs = [
            grade_analysis(
                run["analysis"],
                label,
                turns=sum("error" not in turn for turn in run["transcript"]),
                measured=run["measures"],
            )
            | {"rep": run["rep"]}
            for run in row["runs"]
            if "analysis" in run
        ]
        fault = label["kind"] == "fault"
        complete = len(runs) == reps and all(run["answered"] for run in runs)
        graded.append(
            {
                "case_id": row["case_id"],
                "kind": label["kind"],
                "family": label["family"],
                "variant": label["variant"],
                "fault_lines": label["fault_lines"],
                "rules": grade_analysis(row["rules"], label, turns=None, measured=None),
                "runs": runs,
                "not_run": [run for run in row["runs"] if "analysis" not in run],
                # By case: a fault is flagged only if every repeat flags it, and a clean
                # control is alarmed if any repeat alarms. Both lean against the model.
                "model": {
                    "answered": complete,
                    "flagged": all(bool(run["flagged"]) for run in runs)
                    if fault and complete
                    else None,
                    # An unanswered analysis of a clean control has not shown it quiet.
                    "alarmed": any(bool(run["alarmed"]) or not run["answered"] for run in runs)
                    if not fault and runs
                    else None,
                }
                if model is not None
                else None,
            }
        )
    faults = [c for c in graded if c["kind"] == "fault"]
    clean = [c for c in graded if c["kind"] == "clean"]

    def arm(pick: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
        fault_runs = [g for c in faults for g in pick(c)]
        clean_runs = [g for c in clean for g in pick(c)]
        every = fault_runs + clean_runs
        located_faults = [g for g in fault_runs if g["answered"] and g["flagged"] is not None]
        worded_faults = [g for g in fault_runs if g["answered"] and g["anticipated"] is not None]
        answered_clean = [g for g in clean_runs if g["answered"]]
        costs = [Decimal(g["cost_usd"]) for g in every]
        return {
            "analyses": len(every),
            "not_called": sum(not g["called"] for g in every),
            "answered": _rate(sum(bool(g["answered"]) for g in every), len(every)),
            "faults_flagged_of_answered": _rate(
                sum(bool(g["flagged"]) for g in located_faults), len(located_faults)
            ),
            "faults_anticipated_of_answered": _rate(
                sum(bool(g["anticipated"]) for g in worded_faults), len(worded_faults)
            ),
            "clean_alarmed_of_answered": _rate(
                sum(bool(g["alarmed"]) for g in answered_clean), len(answered_clean)
            ),
            "cost_usd": str(sum(costs, Decimal(0))),
            "cost_usd_median": str(statistics.median(costs)) if costs else None,
            "latency_ms_median": statistics.median([g["latency_ms"] for g in every])
            if every
            else None,
        }

    with_model = model is not None
    return {
        "evaluation": "ai-eval-v2: the Change Analyst against the static risk rules",
        "set": cases.name,
        "dry_run": with_model and not live,
        "model": model,
        "effort": effort,
        "reps": reps,
        "prompt_version": ANALYST_PROMPT_VERSION,
        "tool_version": TOOL_VERSION,
        "budget": {
            "approved_usd": str(budget_usd) if live else None,
            "case_cap_usd": str(ANALYST_CASE_CAP_USD),
            "start_usd": str(ANALYSIS_START_USD),
            "max_output_tokens": ANALYST_MAX_TOKENS,
        },
        "provenance": provenance(cases),
        "cases": graded,
        "summary": {
            "faults": len(faults),
            "clean": len(clean),
            "analyses_expected": len(graded) * reps if with_model else 0,
            "analyses_run": sum(len(c["runs"]) for c in graded),
            "rules": arm(lambda c: [c["rules"]]),
            "model": arm(lambda c: c["runs"]),
            "by_case": {
                "rules_flagged": sum(bool(c["rules"]["flagged"]) for c in faults),
                "rules_alarmed": sum(bool(c["rules"]["alarmed"]) for c in clean),
                "model_answered": sum(bool(c["model"]["answered"]) for c in graded)
                if with_model
                else None,
                "model_flagged": sum(bool(c["model"]["flagged"]) for c in faults)
                if with_model
                else None,
                "model_alarmed": sum(bool(c["model"]["alarmed"]) for c in clean)
                if with_model
                else None,
            },
        },
    }


def analysis_report(document: Mapping[str, Any]) -> str:
    """The Markdown report of a v2 Change Analyst run."""
    summary = document["summary"]
    by_case = summary["by_case"]
    lines = [
        f"# AI evaluation v2: the Change Analyst on the {document['set']} set",
        "",
        "Generated by `python -m dataproof_benchmark eval2-report`. Do not edit by hand.",
        "",
    ]
    if document["dry_run"]:
        lines += ["**A dry run with a scripted model. These are not results.**", ""]
    if document["set"] == "dev":
        lines += ["**The development set: never a headline, whatever it shows.**", ""]
    lines += [
        f"- Model `{document['model']}`, effort {document['effort']}; prompt "
        f"`{document['prompt_version']}`, tools `{document['tool_version']}`; analyses a "
        f"change: {document['reps']}.",
        f"- {summary['faults']} faults and {summary['clean']} clean controls; "
        f"{summary['analyses_run']} of {summary['analyses_expected']} analyses ran, and "
        f"{summary['model']['not_called']} of those sent no request.",
        "",
        "A risk counts when it is raised at MEDIUM or HIGH confidence. Flagged: one of them "
        "states lines within the faulty statement. Anticipated: the keyword proxy. Alarmed: "
        "any such risk on a clean control. Rates are followed by their 95% Wilson interval.",
        "",
        "| | Rules | Model |",
        "| --- | --- | --- |",
    ]
    for title, key in (
        ("Answered", "answered"),
        ("Faults flagged at the faulty statement, of answers", "faults_flagged_of_answered"),
        ("Faults anticipated by keyword, of answers", "faults_anticipated_of_answered"),
        ("Clean controls alarmed, of answers", "clean_alarmed_of_answered"),
    ):
        lines.append(
            f"| {title} | {_shown(summary['rules'].get(key))} | "
            f"{_shown(summary['model'].get(key))} |"
        )
    lines += [
        f"| Cost, USD: total and median | 0 | {summary['model']['cost_usd']} and "
        f"{summary['model']['cost_usd_median']} |",
        "",
        f"By case (a fault is flagged only if every repeat flags it; a clean control is alarmed "
        f"if any repeat alarms): the model flagged {by_case['model_flagged']} of "
        f"{summary['faults']} faults (the rules {by_case['rules_flagged']}) and alarmed on "
        f"{by_case['model_alarmed']} of {summary['clean']} clean controls (the rules "
        f"{by_case['rules_alarmed']}).",
        "",
        "## Cases",
        "",
    ]
    for case in document["cases"]:
        rules = case["rules"]
        lines += [f"### `{case['case_id']}` ({case['kind']})", ""]
        lines.append(
            f"- **Rules:** flagged {_word(rules['flagged'])}; alarmed {_word(rules['alarmed'])}."
        )
        lines += [f"  - {text}" for text in rules["texts"]]
        for run in case["runs"]:
            lines.append(
                f"- **Model, repeat {run['rep']}:** {run['status']}; flagged "
                f"{_word(run['flagged'])}; alarmed {_word(run['alarmed'])}; "
                f"{run['cost_usd']} USD; {run['latency_ms']} ms."
            )
            if run["failure"]:
                lines.append(f"  - No answer: {run['failure']}")
            lines += [f"  - {text}" for text in run["texts"]]
        lines += [f"- Repeat {run['rep']} not run: {run['skipped']}." for run in case["not_run"]]
        lines.append("")
    return "\n".join(lines)


def live_model(model: str, effort: str, max_tokens: int) -> InvestigatorModel:
    """A pinned model with no fallbacks, so that every turn has a cost ceiling."""
    import anthropic

    from dataproof_investigator.anthropic_model import PRICES, AnthropicModel

    if model not in PRICES:
        raise SystemExit(f"no price is recorded for {model}, so its spend could not be capped")
    # The SDK's own retries are off: a request that timed out may have been billed, and a
    # second copy would be billed again, outside the cap. The adapter sends again only what
    # the provider said it did not process.
    return AnthropicModel(
        anthropic.Anthropic(max_retries=0),
        model=model,
        effort=effort,
        max_tokens=max_tokens,
        fallbacks=False,
    )


def analysis_transcripts(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Each analysis's transcript, by case and repeat."""
    return {
        f"{row['case_id']}.rep{run['rep']}": run["transcript"]
        for row in rows
        for run in row["runs"]
        if "transcript" in run
    }
