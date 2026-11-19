"""The second AI evaluation's sets and graders (docs/AI_EVALUATION_V2.md), offline.

No model is called and no database is needed here. These tests hold what makes the held-out set
held out (a model can read no comment, docstring or label), that the graders pass a true answer
and fail an empty one, and that a cap no turn could meet is refused before a run.
"""

import ast
import io
import json
import tarfile
import tokenize
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dataproof_benchmark import ablation, change_analysis, evaluation
from dataproof_benchmark.corpus import CORPUS_SEED, build_corpus
from dataproof_contracts import InvestigationStatus
from dataproof_investigator import (
    Investigator,
    ScriptedModel,
    ToolContext,
    answer,
    change_analyst,
)
from dataproof_investigator.analyst import ANALYST_PROMPT, ANALYST_TOOLS
from dataproof_investigator.anthropic_model import PRICES
from dataproof_investigator.prompts import SYSTEM_PROMPT
from dataproof_investigator.tools import TOOL_SPECS

MANIFEST = json.loads(evaluation.HELDOUT_LABELS.read_text("utf-8"))
HELDOUT = evaluation.HeldoutSet()
LABELS = HELDOUT.labels()
FAULTS = sorted(case_id for case_id, label in LABELS.items() if label["kind"] == "fault")

#: One true sentence about each held-out fault, as a reviewer who knew the cause would put it.
#: The rubric must pass it: a grader that fails the right answer measures nothing.
TRUE_CAUSES = {
    "held-02": "The total is recomputed through float8, and binary floating point loses a cent.",
    "held-03": "The columns pass through timestamp under a Europe/Berlin session time zone.",
    "held-05": "The join to tax_rates matches two rate rows per invoice, so the sum counts both.",
    "held-06": "Each batch window excludes its own start, so the first invoice is never updated.",
    "held-07": "The new columns are declared numeric(12, 0), which cannot hold cents.",
    "held-08": "Unpaid invoices get the sentinel 1970-01-01 instead of staying NULL.",
    "held-10": "The WHERE price_cents > 0 filter leaves the free plan out of plan_catalog.",
    "held-11": "INSERT ... SELECT * copies by position into a table whose column order differs.",
    "held-12": "The guard compares external_ref with =, which never matches NULL, and the "
    "import runs twice.",
    "held-13": "The period columns become timestamp without time zone, which holds no instant.",
    "held-14": "subtotal_cents / 100 is integer division, so the cents are truncated.",
}


def test_the_manifest_covers_every_change_set_of_the_example_once() -> None:
    cases = MANIFEST["cases"]
    changes = sorted(p.name for p in (evaluation.EXAMPLE / "changes").iterdir() if p.is_dir())
    assert sorted(c["change"] for c in cases) == changes
    assert len(cases) == 14
    assert sum(c["kind"] == "fault" for c in cases) == 11
    assert sum(c["kind"] == "clean" for c in cases) == 3
    assert sum(c["origin"] == "sourced" for c in cases) == 8
    for case in cases:
        # A case ID says nothing about its case.
        assert case["case_id"].startswith("held-")
        assert case["case_id"][5:].isdigit()
        if case["kind"] == "fault":
            assert case["rubric"], case["case_id"]
            assert case["fault_markers"], case["case_id"]
    assert set(TRUE_CAUSES) == set(FAULTS)


@pytest.mark.parametrize("case_id", sorted(LABELS))
def test_what_a_model_can_read_of_a_heldout_case_says_nothing_about_it(case_id: str) -> None:
    with tarfile.open(fileobj=io.BytesIO(HELDOUT.archive(case_id)), mode="r:") as archive:
        files = {
            member.name: archive.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in archive.getmembers()
        }
    # Three files under neutral names: the environment, the base schema and the change.
    assert sorted(files) == [
        "migrations/env.py",
        "migrations/versions/0001_base.py",
        evaluation.HELDOUT_REVISION,
    ]
    assert list(HELDOUT.changed(case_id)) == [evaluation.HELDOUT_REVISION]
    change = str(
        MANIFEST["cases"][[c["case_id"] for c in MANIFEST["cases"]].index(case_id)]["change"]
    )
    for name, text in files.items():
        tree = ast.parse(text)  # still a valid module
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.FunctionDef):
                assert ast.get_docstring(node) is None, name
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        assert not [t for t in tokens if t.type == tokenize.COMMENT], name
        assert change not in text, "the change set's name is not in what a model reads"
        for telling in ("Sourced", "sourced", "fault", "incident", "wrong", "bug"):
            assert telling not in text, (name, telling)


def test_blinding_removes_comments_and_docstrings_and_nothing_else() -> None:
    source = (
        '"""Converts cents. The division is integer division: a fault."""\n'
        "\n"
        "from alembic import op\n"
        "\n"
        "\n"
        "def upgrade() -> None:\n"
        '    """Run it."""\n'
        "    # integer / integer is integer division\n"
        '    op.execute("UPDATE t SET a = b / 100")  # loses the cents\n'
        '    op.execute("""\n'
        "        SELECT 1  -- kept: it is inside the statement\n"
        '    """)\n'
    )
    assert evaluation.blind(source) == (
        "from alembic import op\n"
        "\n"
        "\n"
        "def upgrade() -> None:\n"
        '    op.execute("UPDATE t SET a = b / 100")\n'
        '    op.execute("""\n'
        "        SELECT 1  -- kept: it is inside the statement\n"
        '    """)\n'
    )


@pytest.mark.parametrize("case_id", FAULTS)
def test_every_heldout_fault_has_lines_and_they_hold_its_statement(case_id: str) -> None:
    label = LABELS[case_id]
    start, end = label["fault_lines"]
    (revision,) = HELDOUT.changed(case_id).values()
    statement = "\n".join(revision.splitlines()[start - 1 : end])
    case = next(c for c in MANIFEST["cases"] if c["case_id"] == case_id)
    for marker in case["fault_markers"]:
        assert marker in statement, (marker, statement)
    assert end - start < evaluation.MAX_LOCATED_WIDTH, "narrow enough to be located"


@pytest.mark.parametrize("case_id", FAULTS)
def test_the_rubric_passes_the_true_cause_and_fails_no_answer(case_id: str) -> None:
    label = LABELS[case_id]
    assert evaluation.rubric(label["rubric"], [TRUE_CAUSES[case_id]]) is True
    for empty in ("", "I could not determine the cause.", "The check failed with mismatches."):
        assert evaluation.rubric(label["rubric"], [empty]) is False, empty
    # A true cause of another case does not pass for this one, in all but a few pairs.
    others = [
        evaluation.rubric(label["rubric"], [text])
        for c, text in TRUE_CAUSES.items()
        if c != case_id
    ]
    assert sum(bool(o) for o in others) <= 2, others


def test_lines_are_located_only_when_stated_narrowly_and_on_the_statement() -> None:
    fault = (22, 30)
    assert evaluation.stated_ranges("see lines 22-30 and line 41; also 0002_change.py:25") == [
        (22, 30),
        (41, 41),
        (25, 25),
    ]
    assert evaluation.stated_ranges("L22-L30, lines 5 to 9, lines 12 through 14") == [
        (22, 30),
        (5, 9),
        (12, 14),
    ]
    assert evaluation.located(["The UPDATE at lines 24-27 divides integers."], fault) is True
    assert evaluation.located(["migrations/versions/0002_change.py:30"], fault) is True
    assert evaluation.located(["The file, lines 1-200, is at fault."], fault) is False
    assert evaluation.located(["The ALTER at lines 5-9."], fault) is False
    assert evaluation.located(["The division loses 1999 cents on 187 invoices."], fault) is False
    assert evaluation.located(["lines 24-27"], None) is None


def test_the_wilson_interval_is_honest_at_small_counts() -> None:
    assert evaluation.wilson(0, 0) is None
    low, high = evaluation.wilson(11, 11) or (0, 0)
    assert (round(low, 2), high) == (0.74, 1.0), "eleven of eleven does not prove more than 74%"
    low, high = evaluation.wilson(30, 33) or (0, 0)
    assert 0.76 < low < 0.77
    assert 0.96 < high < 0.98
    low, high = evaluation.wilson(1, 18) or (0, 0)
    assert round(low, 3) == 0.01
    assert round(high, 2) == 0.26


def test_the_development_set_is_new_instances_of_the_first_evaluations_templates(
    tmp_path: Path,
) -> None:
    dev = evaluation.DevSet(tmp_path)
    labels = dev.labels()
    assert dev.seed != CORPUS_SEED
    assert len(labels) == 31
    assert sum(label["kind"] == "fault" for label in labels.values()) == 20
    first = {case.revision for case in build_corpus()}
    revisions = {path.split("/")[-1].split("_")[0] for c in labels for path in dev.changed(c)}
    assert len(revisions) == 31
    assert not revisions & first, "no revision of the first evaluation is reused"
    for case_id, label in labels.items():
        assert case_id.startswith("dev-")
        assert (label["fault_lines"] is not None) == (label["kind"] == "fault"), case_id
        (path,) = dev.changed(case_id)
        assert path.startswith(dev.source_roots)


def test_the_investigators_prompts_and_tools_name_nothing_of_the_heldout_schema() -> None:
    """The held-out set is another schema. If its words were in a prompt or a tool
    description, the investigator would have been written with it in view."""
    said = SYSTEM_PROMPT + ANALYST_PROMPT + " ".join(t.description for t in TOOL_SPECS)
    said += " ".join(t.description for t in ANALYST_TOOLS)
    for word in ("invoice", "subscription", "plan_catalog", "tax_rate", "billing", "paid_at"):
        assert word not in said.lower(), word
    # Nor of the development set's.
    for word in ("ledger", "legacy_payment", "refund", "capture"):
        assert word not in said.lower(), word


def context() -> ToolContext:
    return ToolContext(checks={}, evidence={}, source_archive=b"", source_roots=(), databases={})


def test_a_cap_that_no_turn_could_meet_is_refused_before_anything_is_sent() -> None:
    def investigator(ceiling: Decimal | None) -> Investigator:
        return Investigator(ScriptedModel([answer("{}")], turn_ceiling_usd=ceiling))

    # The first evaluation's Change Analyst: 0.194055 USD a turn against 0.15 USD a case.
    with pytest.raises(SystemExit, match="cannot work"):
        ablation.require_feasible_cap(investigator(Decimal("0.194055")), context(), Decimal("0.15"))
    # One worst-case turn fits, two do not: still refused.
    with pytest.raises(SystemExit, match="at least 2"):
        ablation.require_feasible_cap(investigator(Decimal("0.194055")), context(), Decimal("0.30"))
    ablation.require_feasible_cap(investigator(Decimal("0.194055")), context(), Decimal("0.50"))
    ablation.require_feasible_cap(investigator(None), context(), Decimal("0.01"))


def test_a_dry_run_now_meets_a_live_turns_price() -> None:
    assert PRICES[ablation.MODEL][1] == ablation.OUTPUT_USD_PER_MILLION
    assert Decimal("0.16") == ablation.DRY_RUN_CEILING_USD
    turns = ablation.MIN_TURNS_COVERED
    # The second evaluation's caps hold two worst-case turns of output alone.
    assert ablation.DRY_RUN_CEILING_USD * turns <= ablation.CASE_CAP_USD
    analyst_turn = (
        ablation.DRY_RUN_CEILING_USD * evaluation.ANALYST_MAX_TOKENS / ablation.MAX_TOKENS
    )
    assert analyst_turn * turns <= evaluation.ANALYST_CASE_CAP_USD
    # The first evaluation's analyst cap does not hold one, which a dry run would now show.
    assert ablation.DRY_RUN_CEILING_USD > change_analysis.CASE_CAP_USD
    # At the first evaluation's cap even the analyst's smaller turn is refused: 2 x 0.08 > 0.15.
    analyst = change_analyst(evaluation.dry_run_analyst(evaluation.HELDOUT_REVISION))
    with pytest.raises(SystemExit, match="cannot work"):
        ablation.require_feasible_cap(analyst, context(), change_analysis.CASE_CAP_USD)
    ablation.require_feasible_cap(analyst, context(), evaluation.ANALYST_CASE_CAP_USD)


def investigation(status: str, statements: list[str]) -> dict[str, Any]:
    return {
        "status": status,
        "hypotheses": [{"statement": s, "confidence": "MEDIUM"} for s in statements],
        "suggested_action": None,
        "tool_calls": [],
        "failure": None if status == "COMPLETED" else "stopped",
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": "0", "latency_ms": 0},
    }


MEASURED = {"citations": 0, "valid_citations": 0, "latency_ms": 0}
MEASURED |= {"cache_write_tokens": 0, "cache_read_tokens": 0}


def test_an_investigation_that_gave_no_answer_is_not_graded_as_a_wrong_one() -> None:
    label = LABELS["held-14"]
    stopped = evaluation.grade(investigation("BUDGET_EXHAUSTED", []), MEASURED, label)
    assert stopped["answered"] is False
    assert (stopped["root_cause"], stopped["root_cause_first"], stopped["located"]) == (
        None,
        None,
        None,
    )
    start, end = label["fault_lines"]
    right = f"The UPDATE at lines {start}-{end} is integer division, which truncates the cents."
    answered = evaluation.grade(investigation("COMPLETED", [right]), MEASURED, label)
    assert (answered["answered"], answered["root_cause"], answered["located"]) == (True,) * 3
    wrong = evaluation.grade(
        investigation("COMPLETED", ["A time zone shifted it."]), MEASURED, label
    )
    assert (wrong["answered"], wrong["root_cause"], wrong["located"]) == (True, False, False)
    # Summaries keep the two apart.
    arm = evaluation._arm([stopped, answered, wrong])
    assert (arm["answered"]["n"], arm["answered"]["of"]) == (2, 3)
    assert (arm["root_cause_of_answered"]["n"], arm["root_cause_of_answered"]["of"]) == (1, 2)
    assert (arm["root_cause_of_all"]["n"], arm["root_cause_of_all"]["of"]) == (1, 3)


def test_an_analysis_that_sent_no_turn_is_not_a_score() -> None:
    clean, fault = LABELS["held-09"], LABELS["held-14"]
    silent = investigation("BUDGET_EXHAUSTED", [])
    for label in (clean, fault):
        graded = evaluation.grade_analysis(silent, label, turns=0, measured=None)
        assert graded["called"] is False
        assert (graded["answered"], graded["alarmed"], graded["anticipated"]) == (False, None, None)
    # The rules call no model, and matching nothing is their answer: no alarm.
    quiet = investigation(InvestigationStatus.NOT_AVAILABLE.value, [])
    graded = evaluation.grade_analysis(quiet, clean, turns=None, measured=None)
    assert (graded["called"], graded["answered"], graded["alarmed"]) == (True, True, False)
    # A model's LOW-confidence note on a clean change is not an alarm; MEDIUM is.
    low = investigation("COMPLETED", ["Looks safe."])
    low["hypotheses"][0]["confidence"] = "LOW"
    assert evaluation.grade_analysis(low, clean, turns=2, measured=None)["alarmed"] is False
    medium = investigation("COMPLETED", ["The division may truncate."])
    assert evaluation.grade_analysis(medium, clean, turns=2, measured=None)["alarmed"] is True
    assert evaluation.grade_analysis(medium, fault, turns=2, measured=None)["anticipated"] is True


def test_a_dry_run_analyzes_every_heldout_case_without_a_database() -> None:
    cases = evaluation.HeldoutSet()
    rows = evaluation.analyze(cases, evaluation.dry_run_analyst, Decimal(10))
    document = evaluation.analysis_results(
        rows, cases, live=False, model="scripted:dry-run", reps=1, budget_usd=None
    )
    summary = document["summary"]
    assert document["dry_run"] is True
    assert (summary["faults"], summary["clean"]) == (11, 3)
    assert summary["model"]["analyses"] == 14
    assert summary["model"]["not_called"] == 0
    assert summary["model"]["answered"]["n"] == 14
    assert summary["model"]["cost_usd"] == "0"
    assert summary["rules"]["analyses"] == 14
    assert summary["rules"]["answered"]["n"] == 14
    assert len(evaluation.analysis_transcripts(rows)) == 14
    for row in rows:
        (run,) = row["runs"]
        assert run["analysis"]["status"] == "COMPLETED", run["analysis"]["failure"]
        assert run["measures"]["valid_citations"] == run["measures"]["citations"] == 1


def test_a_stage_whose_plumbing_failed_is_not_a_measurement() -> None:
    pinned = "anthropic:claude-opus-5-5"
    good: list[dict[str, Any]] = [
        {"served_by": pinned, "cache_read_tokens": 0},
        {"served_by": pinned, "cache_read_tokens": 2136},
    ]
    assert evaluation.health({"held-14.rep1": good}, pinned) == []
    # A dry run has no pinned model and no cache to read.
    assert (
        evaluation.health({"held-14.rep1": [{"served_by": None}, {"served_by": None}]}, None) == []
    )
    kept: dict[str, list[dict[str, Any]]] = {
        "held-02.rep1": [],
        "held-03.rep1": [
            good[0],
            {"served_by": "anthropic:claude-opus-4-8", "cache_read_tokens": 9},
        ],
        "held-05.rep1": [good[0], {"served_by": pinned, "cache_read_tokens": 0}],
        "held-06.rep1": [{"error": "APIStatusError: overloaded"}],
    }
    assert evaluation.health(kept, pinned) == [
        "held-02.rep1: no request was sent",
        "held-03.rep1 turn 2: served by anthropic:claude-opus-4-8",
        "held-05.rep1 turn 2: nothing was read from the cache",
        "held-06.rep1 turn 1: APIStatusError: overloaded",
    ]
