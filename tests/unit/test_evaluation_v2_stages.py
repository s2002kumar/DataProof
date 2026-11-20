"""The second evaluation's stages, offline: the journal, exit codes, gates, verdicts, the sheet
and the launcher (docs/AI_EVALUATION_V2.md).

What the first evaluation's launcher and gates got wrong is held here: a stage that called no
model passed its gate; a crash would have left the spend unknown; nothing kept a held-out
stage from running twice. No model is called and no database is needed.
"""

import json
import re
import stat
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dataproof_benchmark import evaluation, gates, launcher, verdicts
from dataproof_benchmark.__main__ import (
    EXIT_INCOMPLETE,
    EXIT_NOT_A_MEASUREMENT,
    EXIT_STOP_RULE,
    stage_exit,
)

PINNED = "anthropic:claude-opus-5-5"


# The journal -------------------------------------------------------------------------------------


def test_a_journal_keeps_every_turn_as_it_happens_and_writes_nothing_twice(tmp_path: Path) -> None:
    journal = evaluation.Journal(tmp_path / "journal")
    write = journal.start("dev-101.rep1")
    write({"stop": "tool_use", "cost_usd": "0.01"})
    # The process dies here: the investigation started and never ended.
    with (tmp_path / "journal" / "dev-101.rep1.turns.jsonl").open("a", encoding="utf-8") as file:
        file.write('{"stop": "tool_u')  # a line cut off as it was written
    assert journal.finished("dev-101.rep1") is None
    turns = journal.interrupted("dev-101.rep1")
    assert turns is not None
    assert turns[0] == {"stop": "tool_use", "cost_usd": "0.01"}
    assert "cut off" in turns[1]["error"]
    with pytest.raises(FileExistsError):
        journal.start("dev-101.rep1")
    journal.finish("dev-101.rep1", {"rep": 1})
    with pytest.raises(FileExistsError):
        journal.finish("dev-101.rep1", {"rep": 1})
    assert journal.interrupted("dev-101.rep1") is None


def record(status: str = "COMPLETED", cost: str = "0.10") -> dict[str, Any]:
    return {
        "status": status,
        "tool_calls": [],
        "usage": {"cost_usd": cost, "latency_ms": 1000},
    }


def test_a_stage_stops_at_its_budget_its_stop_rule_and_a_fault_in_the_plumbing() -> None:
    stage = evaluation._Stage(
        "investigation",
        Decimal("1.00"),
        Decimal("1.00"),
        Decimal("0.60"),
        served_by=PINNED,
        stop_after_unanswered=2,
    )
    assert stage.blocked() is None
    assert stage.limited(evaluation.BUDGET).max_cost_usd == Decimal("1.00")
    good = [{"served_by": PINNED, "cost_usd": "0.10"}]
    stage.count("a.rep1", {"investigation": record(), "transcript": good})
    # Never more than the stage has left: its budget is a limit no investigation passes.
    assert stage.limited(evaluation.BUDGET).max_cost_usd == Decimal("0.90")
    # A request that got no answer counts at the most it could have cost.
    lost = [{"error": "ReadTimeout", "unconfirmed_usd": "0.25"}]
    stage.count("b.rep1", {"investigation": record("NOT_AVAILABLE", "0"), "transcript": lost})
    assert stage.spent == Decimal("0.35")
    assert stage.blocked() is not None
    assert (stage.blocked() or "").startswith(evaluation.HALTED), "a failed request halts it"

    budget = evaluation._Stage("investigation", Decimal("1.00"), Decimal("1.00"), Decimal("0.60"))
    budget.count("a.rep1", {"investigation": record(cost="0.45"), "transcript": good})
    assert budget.blocked() == "the budget left cannot cover it"

    rule = evaluation._Stage(
        "investigation", Decimal(10), Decimal(1), Decimal("0.6"), stop_after_unanswered=1
    )
    rule.count("a.rep1", {"investigation": record("BUDGET_EXHAUSTED"), "transcript": good})
    assert rule.blocked() == evaluation.STOPPED


# Exit codes --------------------------------------------------------------------------------------


def document(**summary: Any) -> dict[str, Any]:
    return {"summary": summary, "cases": []}


def test_a_stage_exits_zero_only_when_it_ran_everything_it_was_to_run() -> None:
    assert stage_exit(document(investigations_expected=4, investigations_run=4), []) == 0
    assert stage_exit(document(investigations_expected=4, investigations_run=4), ["x"]) == (
        EXIT_NOT_A_MEASUREMENT
    )
    # Nothing to investigate, or nothing ran: the first evaluation's stages exited 0 here.
    assert stage_exit(document(investigations_expected=0, investigations_run=0), []) == (
        EXIT_NOT_A_MEASUREMENT
    )
    assert stage_exit(document(analyses_expected=28, analyses_run=26), []) == EXIT_INCOMPLETE
    stopped = document(investigations_expected=4, investigations_run=2)
    stopped["cases"] = [
        {"case_id": "a", "runs": [], "not_run": [{"rep": 2, "skipped": evaluation.STOPPED}]}
    ]
    assert stage_exit(stopped, []) == EXIT_STOP_RULE


# Gates -------------------------------------------------------------------------------------------


def turn(cost: str, read: int = 0, *, written: int = 0, out: int = 100) -> dict[str, Any]:
    plain = 1000
    return {
        "served_by": PINNED,
        "input_tokens": plain + written + read,
        "output_tokens": out,
        "cache_write_tokens": written,
        "cache_read_tokens": read,
        "cost_usd": cost,
    }


#: Two turns whose costs agree with their tokens at Opus 5.5's prices.
COSTED = [turn("0.016", written=2000), turn("0.0064", read=2000)]


def stage_document(answered: bool = True, expected: int = 1) -> dict[str, Any]:
    run = {
        "rep": 1,
        "status": "COMPLETED" if answered else "BUDGET_EXHAUSTED",
        "answered": answered,
        "cost_usd": "0.0224",
        "latency_ms": 5000,
    }
    return {
        "dry_run": False,
        "model": PINNED,
        "prompt_version": "p",
        "cases": [{"case_id": "dev-101", "runs": [run], "not_run": []}],
        "summary": {"investigations_expected": expected, "investigations_run": 1},
    }


def test_a_gate_adds_the_spend_up_itself_from_the_tokens() -> None:
    # 1,000 tokens at $4/M, 2,000 written at $5/M and 100 out at $20/M.
    assert gates.turn_cost(COSTED[0]) == Decimal("0.016")
    assert gates.turn_cost(COSTED[1]) == Decimal("0.0064")
    verdict = gates.gate(
        stage_document(),
        {"dev-101.rep1": COSTED},
        kind="pilot",
        cap_usd=Decimal("1"),
        live=True,
        model=PINNED,
        versions={"prompt_version": "p"},
        need_cache_read=True,
        complete=True,
    )
    assert verdict["pass"], verdict["reasons"]
    assert verdict["spend"]["recomputed_usd"] == "0.0224"

    misreported = [COSTED[0], COSTED[1] | {"cost_usd": "0.0001"}]
    failed = gates.gate(
        stage_document(),
        {"dev-101.rep1": misreported},
        kind="stage",
        cap_usd=Decimal("1"),
        live=True,
        model=PINNED,
    )
    assert not failed["pass"]
    assert "recorded 0.0001, recomputed 0.0064" in failed["reasons"][0]


def test_a_gate_fails_a_stage_that_called_no_model() -> None:
    """The first evaluation's gate passed its Change Analyst stage: 34 analyses, no request."""
    verdict = gates.gate(
        stage_document(),
        {"dev-101.rep1": []},
        kind="stage",
        cap_usd=Decimal("1"),
        live=True,
        model=PINNED,
    )
    assert not verdict["pass"]
    assert "dev-101.rep1: no request was sent" in verdict["reasons"]
    empty = stage_document()
    empty["cases"] = []
    assert (
        "no investigation was run"
        in gates.gate(empty, {}, kind="stage", cap_usd=Decimal("1"), live=True, model=PINNED)[
            "reasons"
        ]
    )


def test_a_pilot_that_did_not_answer_or_did_not_finish_fails() -> None:
    kept = {"dev-101.rep1": COSTED}
    unanswered = gates.gate(
        stage_document(answered=False),
        kept,
        kind="pilot",
        cap_usd=Decimal("1"),
        live=True,
        model=PINNED,
    )
    assert "dev-101.rep1: the pilot did not answer (BUDGET_EXHAUSTED)" in unanswered["reasons"]
    partial = gates.gate(
        stage_document(expected=2),
        kept,
        kind="pilot",
        cap_usd=Decimal("1"),
        live=True,
        model=PINNED,
        complete=True,
    )
    assert "1 of the 2 investigations it was to run ran" in partial["reasons"]


def test_a_request_that_got_no_answer_counts_against_the_cap() -> None:
    lost = [*COSTED, {"error": "ReadTimeout", "unconfirmed_usd": "0.98"}]
    verdict = gates.gate(
        stage_document(),
        {"dev-101.rep1": lost},
        kind="stage",
        cap_usd=Decimal("1"),
        live=True,
        model=PINNED,
    )
    assert any("more than the 1 USD cap" in reason for reason in verdict["reasons"])
    assert verdict["spend"]["unconfirmed_usd"] == "0.98"


# Verdicts ----------------------------------------------------------------------------------------


def heldout_document(*, answered: int, cost: str = "0.10", live: bool = True) -> dict[str, Any]:
    """A held-out results document: 11 faults, 3 repeats, ``answered`` of the 33 answered."""
    cases = []
    count = 0
    for number in range(11):
        runs = []
        for rep in (1, 2, 3):
            count += 1
            yes = count <= answered
            runs.append(
                {
                    "rep": rep,
                    "answered": yes,
                    "turns": 4,
                    "located": yes and number < 6,
                    "answer": {"hypotheses": [{"statement": "s", "confidence": "HIGH"}]}
                    if yes
                    else None,
                }
            )
        cases.append(
            {
                "case_id": f"held-{number:02d}",
                "kind": "fault",
                "rules": {"root_cause": number < 3, "answer": {"hypotheses": []}},
                "runs": runs,
                "model": {"located": number < 6},
            }
        )
    return {
        "set": "heldout",
        "dry_run": not live,
        "reps": 3,
        "cases": cases,
        "plumbing": {"problems": []},
        "summary": {
            "investigations_expected": 33,
            "investigations_run": 33,
            "model": {
                "answered": {"n": answered},
                "specifics": 40,
                "unsupported_specifics": 1,
                "cost_usd_median": cost,
                "latency_ms_median": 30_000,
            },
            "by_case": {
                "model_located": 6,
                "model_named_root_cause": 0,
                "model_only": 0,
                "rules_only": 0,
            },
        },
    }


def test_no_criterion_is_met_by_a_run_that_is_not_the_one_specified() -> None:
    found = verdicts.verdict(heldout_document(answered=33, live=False))
    assert found["outcome"].startswith("none")
    assert all(entry["met"] is None for entry in found["criteria"])


def test_the_outcome_follows_the_criteria_and_waits_for_the_owners_marks() -> None:
    finishing = verdicts.verdict(heldout_document(answered=31))
    met = {entry["criterion"][:2]: entry["met"] for entry in finishing["criteria"]}
    assert (met["A1"], met["A2"], met["A4"], met["A5"], met["A6"]) == (True,) * 5
    assert (met["A3"], met["A7"]) == (None, None), "a cause is the owner's reading"
    assert finishing["outcome"] == "undecided until the owner's marks are in"

    assert verdicts.verdict(heldout_document(answered=29))["outcome"] == "disabled"
    costly = verdicts.verdict(heldout_document(answered=31, cost="0.20"))
    assert {e["criterion"][:2]: e["met"] for e in costly["criteria"]}["A5"] is False


def test_the_sheet_hides_the_arm_and_the_marks_decide_the_cause() -> None:
    improved, old = heldout_document(answered=33), heldout_document(answered=0)
    references = {
        f"held-{n:02d}": {
            "family": "a family",
            "origin": "sourced",
            "fault_lines": [3, 4],
            "statement": ["op.execute('x')"],
        }
        for n in range(11)
    }
    items, key = verdicts.sheet({"improved": improved, "old": old}, references)
    assert len(items) == 11 + 33, "the rules once per case, and every answer"
    sheet = verdicts.sheet_markdown(items)
    for arm in ("improved", "old", "rules"):
        assert not re.search(rf"{arm}", sheet), arm
    assert {entry["arm"] for entry in key.values()} == {"rules", "improved"}
    assert [i["item"] for i in items] == sorted(i["item"] for i in items)
    # The order is shuffled, and fixed: the same seed gives the same sheet.
    assert verdicts.sheet({"improved": improved, "old": old}, references)[1] == key

    # The owner marks the improved arm's answers as naming the cause in 8 cases, of which the
    # rules name 2; the rules name a third alone.
    rows = ["item,cause,statement,actionable,grounded,note"]
    for item, entry in key.items():
        case = int(entry["case_id"][5:])
        cause = case < 8 if entry["arm"] == "improved" else case in (0, 1, 9)
        rows.append(f"{item},{'y' if cause else 'n'},y,y,y,")
    marks = verdicts.read_marks("\n".join(rows) + "\n")
    found = verdicts.verdict(improved, marks, key)
    a3 = next(e for e in found["criteria"] if e["criterion"].startswith("A3"))
    assert a3["met"] is True, a3
    assert "named in 8 of 11, 6 of them cases the rules miss, 1 where only the rules" in a3["value"]
    assert found["outcome"] == "optional"


def test_an_unmarked_answer_leaves_the_cause_undecided() -> None:
    improved = heldout_document(answered=33)
    references = {
        f"held-{n:02d}": {"family": "f", "origin": "o", "fault_lines": [1, 1], "statement": []}
        for n in range(11)
    }
    _, key = verdicts.sheet({"improved": improved}, references)
    marks = verdicts.read_marks("item,cause,statement,actionable,grounded,note\n")
    found = verdicts.verdict(improved, marks, key)
    assert found["outcome"] == "undecided until the owner's marks are in"


# The launcher ------------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[launcher.State]:
    monkeypatch.setattr(launcher, "commit", lambda *_: "c" * 40)
    yield launcher.State(tmp_path / "state")
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def attempt(
    state: launcher.State,
    name: str,
    *,
    passed: bool = True,
    commit: str = "c" * 40,
    costs: tuple[str, ...] = ("0.10",),
    sealed: bool = True,
) -> launcher.Attempt:
    folder = state.stages / f"{len(state.attempts()) + 1:02d}-{name}"
    (folder / "journal").mkdir(parents=True)
    started = {"stage": name, "commit": commit, "budget_usd": "1.00", "started_at": "t"}
    (folder / "started.json").write_text(json.dumps(started), "utf-8")
    role = launcher.PLANS[name].role
    for number, cost in enumerate(costs, 1):
        key = f"case-{number}.rep1"
        (folder / "journal" / f"{key}.turns.jsonl").write_text(
            '{"started": "x"}\n' + json.dumps({"cost_usd": cost}) + "\n", "utf-8"
        )
        run = {"investigation" if role == "investigator" else "analysis": record(cost=cost)}
        (folder / "journal" / f"{key}.run.json").write_text(json.dumps(run), "utf-8")
    made = launcher.Attempt(folder)
    if sealed:
        launcher.seal(state, made, {"pass": passed}, abandoned=False)
    return made


def refused(state: launcher.State, name: str) -> str:
    with pytest.raises(launcher.Refused) as reason:
        launcher.check(state, launcher.PLANS[name])
    return str(reason.value)


def test_nothing_runs_before_its_pilot_has_passed_at_the_commit_being_run(
    state: launcher.State,
) -> None:
    assert "needs dev-pilot to have passed at this commit" in refused(state, "dev")
    attempt(state, "dev-pilot", commit="d" * 40)
    assert "needs dev-pilot to have passed at this commit" in refused(state, "dev")
    attempt(state, "dev-pilot", passed=False)
    assert "needs dev-pilot to have passed" in refused(state, "dev")
    attempt(state, "dev-pilot")
    head, budget = launcher.check(state, launcher.PLANS["dev"])
    # The pool's 2.50 less what three pilots spent.
    assert (head, budget) == ("c" * 40, Decimal("2.20"))
    assert "needs dev to have passed its gate first" in refused(state, "heldout")


def test_a_heldout_stage_runs_once_and_only_if_its_budget_covers_it(
    state: launcher.State,
) -> None:
    attempt(state, "dev-pilot")
    attempt(state, "dev", costs=("0.30",) * 16)
    reason = refused(state, "heldout")
    assert "need about 10.20 USD, and it would be given 5.00" in reason, reason
    assert "the owner's decision" in reason

    cheap = launcher.State(state.root.with_name("cheap"))
    attempt(cheap, "dev-pilot")
    attempt(cheap, "dev", costs=("0.10",) * 16)
    assert launcher.check(cheap, launcher.PLANS["heldout"])[1] == Decimal("5.00")
    attempt(cheap, "heldout")
    assert "runs once" in refused(cheap, "heldout")


def test_an_open_stage_blocks_everything_and_closing_it_charges_what_it_could_have_spent(
    state: launcher.State,
) -> None:
    attempt(state, "dev-pilot")
    open_one = attempt(state, "dev", sealed=False)
    # An investigation that was running when the process ended: 0.12 USD on disk.
    (open_one.journal / "case-9.rep1.turns.jsonl").write_text(
        '{"started": "x"}\n{"cost_usd": "0.12"}\n', "utf-8"
    )
    assert "started and never closed" in refused(state, "heldout")
    # While open, the stage is charged its whole budget.
    assert state.charged("development") == Decimal("1.10")
    assert launcher.abandon(state) == 0
    sealed = open_one.sealed or {}
    assert sealed["abandoned"] is True
    # The request in flight could have cost at most the cap less what that investigation spent.
    assert sealed["in_flight_allowance_usd"] == "0.88"
    assert sealed["charged_usd"] == "1.10"
    assert "needs dev to have passed" in refused(state, "heldout")


def test_a_record_changed_after_its_seal_is_found(state: launcher.State) -> None:
    made = attempt(state, "dev-pilot")
    text, problems = launcher.status(state)
    assert problems == []
    assert "dev-pilot" in text
    target = made.journal / "case-1.rep1.run.json"
    target.chmod(stat.S_IREAD | stat.S_IWRITE)
    target.write_text("{}", "utf-8")
    (made.folder / "added.txt").write_text("x", "utf-8")
    _, problems = launcher.status(state)
    assert any("changed since it was sealed" in p for p in problems)
    assert any("added after the seal" in p for p in problems)
    assert "have changed" in refused(state, "dev")


def test_the_pools_add_up_to_the_owners_limits() -> None:
    assert Decimal("13.50") == launcher.TOTAL
    assert launcher.POOLS["development"] == Decimal("2.50")
    assert launcher.POOLS["heldout"] == Decimal("5.00")
    assert launcher.POOLS["old-arm"] == Decimal("2.00")
    assert launcher.POOLS["analyst-development"] + launcher.POOLS["analyst-heldout"] == (
        Decimal("4.00")
    )
    assert {plan.name for plan in launcher.PLANS.values() if plan.once} == {
        "heldout",
        "old-arm",
        "analyst-heldout",
    }


def test_a_paid_stage_is_refused_without_a_key_or_the_exact_approval(
    state: launcher.State,
) -> None:
    with pytest.raises(launcher.Refused, match="needs the API key"):
        launcher.run(state, "dev-pilot", Decimal("1.00"), "dsn", None)
    with pytest.raises(launcher.Refused, match=r"would be given 1.00 USD; --approve said 2"):
        launcher.run(state, "dev-pilot", Decimal(2), "dsn", "sk-ant-not-a-real-key")
    assert state.attempts() == [], "nothing was started"


def models_api(status: int, body: dict[str, Any]) -> Any:
    import anthropic
    import httpx2

    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET", "the check sends nothing billed"
        assert request.url.path.startswith("/v1/models/")
        return httpx2.Response(status, json=body)

    return anthropic.Anthropic(
        api_key="test-key-not-real",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handle)),
    )


def model_info(model_id: str) -> dict[str, Any]:
    return {
        "type": "model",
        "id": model_id,
        "display_name": "x",
        "created_at": "2026-01-01T00:00:00Z",
    }


def test_the_key_and_the_model_are_checked_at_no_cost_before_anything_is_billed() -> None:
    model = "claude-opus-5-5"
    launcher.preflight("k", model, models_api(200, model_info(model)))
    launcher.preflight("k", model, models_api(200, model_info(f"{model}-20261001")))
    with pytest.raises(launcher.Refused, match="named claude-opus-5"):
        launcher.preflight("k", model, models_api(200, model_info("claude-opus-5")))
    error = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    with pytest.raises(launcher.Refused, match="the key was not accepted"):
        launcher.preflight("k", model, models_api(401, error))
    missing = {"type": "error", "error": {"type": "not_found_error", "message": "model: x"}}
    with pytest.raises(launcher.Refused, match="is not served to this key"):
        launcher.preflight("k", model, models_api(404, missing))
