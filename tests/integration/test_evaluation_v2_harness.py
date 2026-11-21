"""The second AI evaluation's harness against real PostgreSQL, with scripted models.

No model is called. The held-out cases are staged as a run stages them (reference, migrated
candidate, verified contracts and type checks), and scripted investigators stand in for a model:
one that says nothing useful, and one that gives the true answer, which the graders must pass.
"""

import json
from decimal import Decimal

import pytest

from dataproof_benchmark import evaluation
from dataproof_investigator import Budget, InvestigatorModel, ScriptedModel, answer, call

pytestmark = pytest.mark.integration

CLEAN, FAULT = "held-09", "held-14"
"""A clean control (cents to decimal money) and its faulty sibling (integer division)."""


def test_a_dry_run_investigates_the_fault_and_skips_the_clean_control(postgres_dsn: str) -> None:
    cases = evaluation.HeldoutSet()
    rows = evaluation.investigate(
        postgres_dsn, cases, evaluation.dry_run_model, Decimal(10), reps=2, case_ids=[CLEAN, FAULT]
    )
    document = evaluation.results(
        rows,
        cases,
        live=False,
        model="scripted:dry-run",
        effort="medium",
        reps=2,
        budget_usd=Decimal(10),
    )
    clean, fault = document["cases"]
    assert (clean["case_id"], clean["skipped"]) == (CLEAN, "no check failed or errored")
    assert clean["runs"] == []
    assert clean["rules"] is None

    assert fault["skipped"] is None
    assert fault["failed"] == ["subscriptions.invoice_amounts"]
    assert fault["rules"]["answered"] is True
    assert fault["rules"]["root_cause"] is True, "the rules see values rounded to whole units"
    assert [run["rep"] for run in fault["runs"]] == [1, 2]
    for run in fault["runs"]:
        assert run["status"] == "COMPLETED", run["failure"]
        assert (run["answered"], run["root_cause"], run["located"]) == (True, False, False)
        # The dry run does what a live investigator is asked to: the briefing, the change.
        assert run["tool_calls"] == {"OK": 2}
        assert (run["valid_citations"], run["citations"]) == (2, 2)
        assert run["turns"] == 3
    assert fault["model"] == {
        "answered": True,
        "root_cause": False,
        "located": False,
        "statement_located": False,
    }
    assert evaluation.uncalled(document) == []
    assert sorted(evaluation.transcripts(rows)) == [f"{FAULT}.rep1", f"{FAULT}.rep2"]

    summary = document["summary"]
    assert (summary["cases"], summary["investigated"]) == (2, 1)
    assert (summary["investigations_expected"], summary["investigations_run"]) == (2, 2)
    assert document["provenance"]["labels_sha256"] is not None
    assert summary["model"]["answered"] == {"n": 2, "of": 2, "wilson_95": [0.342, 1.0]}
    assert summary["by_case"]["rules_only"] == 1
    report = evaluation.report(document)
    assert "**A dry run with a scripted model. These are not results.**" in report
    assert "| Answered | 1 of 1 (21% to 100%) | 2 of 2 (34% to 100%) |" in report
    assert f"### `{CLEAN}`\n\nNot investigated: no check failed or errored." in report


def test_the_graders_pass_a_true_answer_given_through_the_real_tools(postgres_dsn: str) -> None:
    """The known-good run: a scripted investigator that reads the briefing and the migration
    and gives the true cause with its lines. If this did not pass, no live answer could."""
    cases = evaluation.HeldoutSet()
    start, end = cases.labels()[FAULT]["fault_lines"]
    seen: dict[str, ScriptedModel] = {}

    def knowing() -> InvestigatorModel:
        final = {
            "hypotheses": [
                {
                    "statement": f"The UPDATE at {evaluation.HELDOUT_REVISION} lines {start}-{end} "
                    "divides integer cents by 100 without a cast: integer division truncates "
                    "the cents, so 19.99 becomes 19.00.",
                    "cited_evidence_ids": ["investigation.tool.1", "investigation.tool.2"],
                    "confidence": "HIGH",
                }
            ],
            "suggested_action": "Cast to numeric before dividing: subtotal_cents::numeric / 100.",
            "caveat": "Read from the migration and the recorded mismatches.",
        }
        read = {"path": evaluation.HELDOUT_REVISION, "start_line": 1, "end_line": 60}
        seen["model"] = ScriptedModel(
            [call("get_briefing", {}), call("read_source", read), answer(json.dumps(final))]
        )
        return seen["model"]

    rows = evaluation.investigate(postgres_dsn, cases, knowing, Decimal(10), case_ids=[FAULT])
    (case,) = evaluation.scored(rows, cases.labels())
    (run,) = case["runs"]
    assert run["status"] == "COMPLETED", run["failure"]
    assert (run["answered"], run["root_cause"], run["hedged"], run["located"]) == (
        True,
        True,
        False,
        True,
    )
    assert (run["valid_citations"], run["citations"]) == (2, 2)
    assert run["unsupported_specifics"] == [], "its numbers are in the evidence it cites"

    # The investigator's tools work on a schema it was not written against.
    briefing = json.loads(seen["model"].received[1][0].content)["data"]
    assert [c["check_id"] for c in briefing["failed_checks"]] == ["subscriptions.invoice_amounts"]
    assert [c["path"] for c in briefing["changed_files"]] == [evaluation.HELDOUT_REVISION]
    assert "invoices" in briefing["tables"]["CANDIDATE"]
    source = json.loads(seen["model"].received[2][0].content)["data"]
    statement = " ".join(text for number, text in source["lines"] if start <= number <= end)
    assert "subtotal_cents / 100" in statement
    assert "integer division" not in json.dumps(source), "the revision's comment is not readable"


def test_a_cap_no_turn_could_meet_stops_the_run_before_it_starts(postgres_dsn: str) -> None:
    def costly() -> InvestigatorModel:
        # Two such turns are more than an investigation's cap of 1.00 USD.
        return ScriptedModel([answer("{}")], turn_ceiling_usd=Decimal("0.60"))

    with pytest.raises(SystemExit, match="cannot work"):
        evaluation.investigate(
            postgres_dsn, evaluation.HeldoutSet(), costly, Decimal(10), case_ids=[FAULT]
        )


def test_the_stop_rule_starts_no_investigation_after_too_many_gave_no_answer(
    postgres_dsn: str,
) -> None:
    def never_answers() -> InvestigatorModel:
        check = {"check_id": "subscriptions.invoice_amounts"}
        return ScriptedModel([call("get_check_result", check, f"t{n}") for n in range(9)])

    cases = evaluation.HeldoutSet()
    rows = evaluation.investigate(
        postgres_dsn,
        cases,
        never_answers,
        Decimal(10),
        reps=3,
        case_ids=[FAULT],
        budget=Budget(max_tool_calls=2, max_turns=6, max_cost_usd=Decimal("0.50")),
        stop_after_unanswered=1,
    )
    document = evaluation.results(
        rows,
        cases,
        live=False,
        model="scripted:test",
        effort="medium",
        reps=3,
        budget_usd=Decimal(10),
    )
    (case,) = document["cases"]
    (run,) = case["runs"]
    assert (run["status"], run["answered"], run["root_cause"]) == ("BUDGET_EXHAUSTED", False, None)
    assert [skipped["skipped"] for skipped in case["not_run"]] == [evaluation.STOPPED] * 2
    assert evaluation.stopped(document) is True
    # An investigation that gave no answer is counted as that, and as nothing else.
    model = document["summary"]["model"]
    assert (model["answered"]["n"], model["answered"]["of"]) == (0, 1)
    assert model["root_cause_of_answered"]["wilson_95"] is None
    assert "No answer: no answer: it was asked for its answer twice" in evaluation.report(document)
