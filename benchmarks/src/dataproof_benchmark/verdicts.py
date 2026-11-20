"""The second evaluation's acceptance criteria, as code (docs/AI_EVALUATION_V2.md).

Written before any live run, so that what a result means is not decided after seeing it. A
criterion is met, not met or undecided. It is undecided when what it needs was not measured
(a stage that is not the one specified, a stage that did not finish, a mark the owner has not
given), and never met by default: a criterion that passes on nothing is how the first
evaluation reported a stage in which no model was called.

Two graders decide. What a machine can decide, it decides: whether an investigation answered,
what it cost, whether it stated only what its evidence shows. Whether an answer names the
cause and points at the faulty statement is the owner's reading, blind to which model arm wrote
it (``sheet``): a review showed the keyword and line-range proxies passing answers that name
and locate nothing, and failing correct ones.
"""

import csv
import hashlib
import io
import random
import secrets
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

HELDOUT_FAULTS = 11
HELDOUT_REPS = 3
PINNED_MODEL = "anthropic:claude-opus-5-5"
EFFORT = "medium"
A1_ANSWERED = 30
A2_UNSUPPORTED = Decimal("0.05")
A3_NAMED = 7
A3_BEYOND_RULES = 4
A3_RULES_ONLY = 1
A4_LOCATED = 6
A5_COST_USD = Decimal("0.15")
A5_SECONDS = 60
A7_ACTIONABLE = Decimal("0.8")

ANALYST_CHANGES = 14
ANALYST_REPS = 2
DEV_CLEAN = 11
C2_ANSWERED = 25
C3_FLAGGED = 6
C4_HELDOUT_ALARMS = 1
C4_DEV_ALARMS = 3

MARKS = ("cause", "statement", "actionable", "grounded")
"""What the owner marks on each answer, y or n: does it name the cause the reference gives;
does it point at the faulty statement; would it have told me what to fix; does it state only
what its evidence shows."""
_YES = frozenset({"y", "yes", "true", "1"})
_NO = frozenset({"n", "no", "false", "0"})
RULES_PREFIX = "rules-"
"""The rules' items are named for their case: they are not blinded."""


def criterion(name: str, met: bool | None, value: str, needs: str) -> dict[str, Any]:
    return {"criterion": name, "met": met, "value": value, "needs": needs}


def labels_sha256() -> str:
    from dataproof_benchmark.evaluation import HELDOUT_LABELS

    return hashlib.sha256(Path(HELDOUT_LABELS).read_bytes()).hexdigest()


# The sheet ---------------------------------------------------------------------------------------


def sheet(
    arms: Mapping[str, Mapping[str, Any]],
    references: Mapping[str, Mapping[str, Any]],
    seed: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every answer to be read, and the key that says which arm wrote which, to be opened only
    when the marks are in.

    ``arms`` is each model arm's results document by its name ("improved", "old"). Their
    answers are shuffled together, without the arm, in an order drawn from a seed that only the
    key holds. The rules' diagnosis of each case is an item too, so that "the rules miss it" is
    the same reader's judgment and not a keyword's; those items are not blinded, because the
    rules' fixed wording gives them away, and the sheet says so.
    """
    seed = secrets.randbits(64) if seed is None else seed
    blinded: list[tuple[str, str, int | None, Mapping[str, Any]]] = []
    rules: list[tuple[str, Mapping[str, Any]]] = []
    for position, (arm, document) in enumerate(arms.items()):
        for case in document["cases"]:
            if case["rules"] is None or case["kind"] != "fault":
                continue
            if position == 0 and case["rules"]["answer"]:
                rules.append((case["case_id"], case["rules"]["answer"]))
            blinded += [
                (arm, case["case_id"], run["rep"], run["answer"])
                for run in case["runs"]
                if run["answer"]
            ]
    random.Random(seed).shuffle(blinded)  # noqa: S311 - an order to read in, kept in the key
    items: list[dict[str, Any]] = []
    entries: dict[str, dict[str, Any]] = {}
    for number, (arm, case_id, rep, answer) in enumerate(blinded, 1):
        name = f"item-{number:03d}"
        items.append({"item": name, "reference": references[case_id], "answer": answer})
        entries[name] = {"arm": arm, "case_id": case_id, "rep": rep}
    for case_id, answer in rules:
        name = f"{RULES_PREFIX}{case_id}"
        items.append(
            {"item": name, "reference": references[case_id], "answer": answer, "rules": True}
        )
        entries[name] = {"arm": "rules", "case_id": case_id, "rep": None}
    return items, {"seed": seed, "items": entries}


def _answer_lines(answer: Mapping[str, Any], *, rules: bool) -> list[str]:
    lines = [
        f"{number}. ({hypothesis['confidence']}) {hypothesis['statement']}"
        for number, hypothesis in enumerate(answer["hypotheses"], 1)
    ]
    if not rules:
        lines += [
            "",
            f"Suggested action: {answer.get('suggested_action') or 'none'}",
            "",
            f"Caveat: {answer.get('caveat') or 'none'}",
        ]
    return lines


def sheet_markdown(items: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "# Answers to mark",
        "",
        "Each item is one answer about one migration. The reference says what is actually "
        "wrong. In `marks.csv`, give each item four marks, `y` or `n`:",
        "",
        "- **cause:** the answer names the cause the reference gives, in its first hypothesis.",
        "- **statement:** it points at the faulty statement.",
        "- **actionable:** it would have told you what to fix.",
        "- **grounded:** it states nothing its evidence could not show.",
        "",
        "Do not open the held-out results, transcripts or reports before every mark is in: they "
        "say which arm wrote what.",
        "",
        "## Blinded: which investigator wrote an answer is not shown",
        "",
    ]
    rules_started = False
    for item in items:
        reference, answer = item["reference"], item["answer"]
        if item.get("rules") and not rules_started:
            rules_started = True
            lines += [
                "## Not blinded: the rule-based diagnosis of each case",
                "",
                "Its fixed wording would give it away, so it is shown as what it is. Mark it as "
                "the others.",
                "",
            ]
        lines += [
            f"### {item['item']}",
            "",
            f"**Reference.** {reference['family']} ({reference['origin']}). The faulty "
            f"statement, lines {reference['fault_lines'][0]} to {reference['fault_lines'][1]} "
            "of the file a model reads:",
            "",
            "```python",
            *reference["statement"],
            "```",
            "",
            "**Answer.**",
            "",
            *_answer_lines(answer, rules=bool(item.get("rules"))),
            "",
        ]
    return "\n".join(lines)


def marks_csv(items: Sequence[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["item", *MARKS, "note"])
    for item in items:
        writer.writerow([item["item"], *[""] * len(MARKS), ""])
    return buffer.getvalue()


def read_marks(text: str) -> dict[str, dict[str, bool | None]]:
    """The owner's marks by item: y, yes, true or 1, and n, no, false or 0, in any case. A blank
    is no mark. Anything else is refused, with where it is: a mark misread is a verdict
    misread. A spreadsheet's byte-order mark and its choice of delimiter are taken as they
    come."""
    text = text.removeprefix("﻿")
    first = text.splitlines()[0] if text else ""
    delimiter = max(",;\t", key=first.count) if first else ","
    marks: dict[str, dict[str, bool | None]] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        item = (row.get("item") or "").strip()
        if not item:
            continue
        found: dict[str, bool | None] = {}
        for name in MARKS:
            value = (row.get(name) or "").strip().lower()
            if not value:
                found[name] = None
            elif value in _YES:
                found[name] = True
            elif value in _NO:
                found[name] = False
            else:
                raise ValueError(f"{item}: {name} is {value!r}; give y or n")
        marks[item] = found
    return marks


# The Failure Investigator ------------------------------------------------------------------------


def _by_run(
    marks: Mapping[str, Mapping[str, bool | None]],
    key: Mapping[str, Any],
    arm: str,
) -> dict[tuple[str, int | None], Mapping[str, bool | None]]:
    return {
        (entry["case_id"], entry["rep"]): marks.get(item, {})
        for item, entry in key["items"].items()
        if entry["arm"] == arm
    }


def specified(document: Mapping[str, Any]) -> list[str]:
    """Why a held-out investigation stage is not the one the protocol specifies; empty if it is."""
    from dataproof_investigator import PROMPT_VERSION

    faults = [c for c in document["cases"] if c["rules"] is not None]
    wrong = []
    if document.get("set") != "heldout":
        wrong.append(f"the set is {document.get('set')}")
    if document.get("dry_run"):
        wrong.append("a dry run")
    if len(faults) != HELDOUT_FAULTS:
        wrong.append(f"{len(faults)} faults investigated, not {HELDOUT_FAULTS}")
    if document.get("reps") != HELDOUT_REPS:
        wrong.append(f"{document.get('reps')} repeats, not {HELDOUT_REPS}")
    if document.get("model") != PINNED_MODEL:
        wrong.append(f"the model is {document.get('model')}")
    if document.get("effort") != EFFORT:
        wrong.append(f"effort {document.get('effort')}")
    if document.get("prompt_version") != PROMPT_VERSION:
        wrong.append(f"prompt {document.get('prompt_version')}, not {PROMPT_VERSION}")
    provenance = document.get("provenance", {})
    if provenance.get("labels_sha256") != labels_sha256():
        wrong.append("labels other than the ones in this checkout")
    if provenance.get("harness_modified"):
        wrong.append("run from a modified checkout")
    return wrong


def outcome(met: Mapping[str, bool | None], first: Sequence[str], void: str) -> str:
    """What the criteria mean, in the order the protocol's table gives them."""
    if met[void] is False:
        return "void: the stage is not a measurement"
    if any(met[name] is False for name in first):
        return "disabled"
    if any(met[name] is None for name in first):
        return "undecided: the stage did not run in full"
    if any(value is False for value in met.values()):
        return "experimental"
    if any(value is None for value in met.values()):
        return "undecided until the owner's marks are in"
    return "optional"


def verdict(
    document: Mapping[str, Any],
    marks: Mapping[str, Mapping[str, bool | None]] | None = None,
    key: Mapping[str, Any] | None = None,
    arm: str = "improved",
) -> dict[str, Any]:
    """A1 to A7 for a held-out investigation stage, and what they mean for the product."""
    summary = document["summary"]
    model = summary["model"]
    faults = [c for c in document["cases"] if c["rules"] is not None]
    reps = document["reps"]
    wrong = specified(document)
    ok = not wrong
    expected = summary["investigations_expected"]
    ran = summary["investigations_run"]
    stopped_early = any(
        str(run.get("skipped", "")).startswith("the stop rule")
        for case in document["cases"]
        for run in case["not_run"]
    )
    complete = ran == expected or stopped_early
    answered = model["answered"]["n"]
    found: list[dict[str, Any]] = []

    found.append(
        criterion(
            "A1, finishes",
            (answered >= A1_ANSWERED) if ok and complete else None,
            f"{answered} of {expected} investigations answered ({ran} ran)",
            f"at least {A1_ANSWERED} of {HELDOUT_FAULTS * HELDOUT_REPS}",
        )
    )
    stated, unsupported = model["specifics"], model["unsupported_specifics"]
    found.append(
        criterion(
            "A2, grounded",
            (Decimal(unsupported) / stated <= A2_UNSUPPORTED) if ok and stated else None,
            f"{unsupported} of {stated} specifics stated in answers are unsupported by the "
            "evidence they cite",
            "at most 5% (undecided if none was stated)",
        )
    )

    # A3, A4 and A7: the owner's marks, by case. A repeat that gave no answer is not marked,
    # and counts as not naming and not locating.
    keyed = marks is not None and key is not None
    ours = _by_run(marks, key, arm) if marks is not None and key is not None else {}
    rules = _by_run(marks, key, "rules") if marks is not None and key is not None else {}
    owed = [(c["case_id"], r["rep"]) for c in faults for r in c["runs"] if r["answer"]]
    owed_rules = [(c["case_id"], None) for c in faults if c["rules"]["answer"]]

    def marked(name: str, runs: Mapping[Any, Mapping[str, bool | None]], keys: Any) -> bool:
        return keyed and all(runs.get(k, {}).get(name) is not None for k in keys)

    def by_case(name: str, case: Mapping[str, Any]) -> bool:
        yes = sum(bool(ours.get((case["case_id"], r["rep"]), {}).get(name)) for r in case["runs"])
        return yes * 2 > int(reps)

    a3 = named = beyond = rules_only = None
    if marked("cause", ours, owed) and marked("cause", rules, owed_rules):
        named = sum(by_case("cause", case) for case in faults)
        beyond = sum(
            by_case("cause", case) and not rules.get((case["case_id"], None), {}).get("cause")
            for case in faults
        )
        rules_only = sum(
            bool(rules.get((case["case_id"], None), {}).get("cause")) and not by_case("cause", case)
            for case in faults
        )
        if ok and complete:
            a3 = named >= A3_NAMED and beyond >= A3_BEYOND_RULES and rules_only <= A3_RULES_ONLY
    proxy = summary["by_case"]
    found.append(
        criterion(
            "A3, adds to the rules",
            a3,
            (
                f"by the owner's marks, by case: named in {named} of {len(faults)}, {beyond} "
                f"of them cases the rules miss, {rules_only} where only the rules name it"
                if named is not None
                else "the owner's marks are not in"
            )
            + f". The keyword proxy: {proxy['model_named_root_cause']} named, "
            f"{proxy['model_only']} beyond the rules, {proxy['rules_only']} rules only",
            f"at least {A3_NAMED} of {HELDOUT_FAULTS}, at least {A3_BEYOND_RULES} of them "
            f"missed by the rules, at most {A3_RULES_ONLY} where only the rules name it",
        )
    )
    located = (
        sum(by_case("statement", case) for case in faults)
        if marked("statement", ours, owed)
        else None
    )
    found.append(
        criterion(
            "A4, locates",
            (located >= A4_LOCATED) if ok and complete and located is not None else None,
            (
                f"by the owner's marks, by case: the faulty statement pointed at in {located} "
                f"of {len(faults)}"
                if located is not None
                else "the owner's marks are not in"
            )
            + f". The line-range proxy: {proxy['model_located']}",
            f"at least {A4_LOCATED} of {HELDOUT_FAULTS}",
        )
    )
    cost = Decimal(model["cost_usd_median"]) if model["cost_usd_median"] else None
    seconds = model["latency_ms_median"] / 1000 if model["latency_ms_median"] is not None else None
    found.append(
        criterion(
            "A5, affordable",
            (cost <= A5_COST_USD and seconds <= A5_SECONDS)
            if ok and complete and cost is not None and seconds is not None
            else None,
            f"median {cost} USD and {seconds} s an investigation",
            f"at most {A5_COST_USD} USD and {A5_SECONDS} s",
        )
    )
    problems = list(document.get("plumbing", {}).get("problems", []))
    unsent = [f"{c['case_id']} rep {r['rep']}" for c in faults for r in c["runs"] if not r["turns"]]
    found.append(
        criterion(
            "A6, as configured",
            (not problems and not unsent) if ok else None,
            f"{len(problems)} plumbing faults; {len(unsent)} investigations sent no request",
            "none of either, on a live run of the held-out stage as specified",
        )
    )
    causes = [k for k in owed if ours.get(k, {}).get("cause")]
    actionable = None
    if causes and all(ours[k].get("actionable") is not None for k in causes):
        actionable = sum(bool(ours[k]["actionable"]) for k in causes)
    found.append(
        criterion(
            "A7, the owner finds it actionable",
            (Decimal(actionable) / len(causes) >= A7_ACTIONABLE)
            if ok and complete and actionable is not None
            else None,
            f"{actionable} of the {len(causes)} answers marked as naming the cause are marked "
            "actionable"
            if actionable is not None
            else "the owner's marks are not in, or no answer was marked as naming the cause",
            "at least 4 in 5",
        )
    )
    met = {entry["criterion"][:2]: entry["met"] for entry in found}
    if wrong:
        result = "none: this is not the held-out stage as specified (" + "; ".join(wrong) + ")"
    else:
        result = outcome(met, ("A1", "A2"), "A6")
    return {"role": "Failure Investigator", "criteria": found, "outcome": result}


# The Change Analyst ------------------------------------------------------------------------------


def analysis_verdict(
    heldout: Mapping[str, Any], dev: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The Change Analyst's criteria on the held-out stage, with the development set's clean
    controls beside the held-out set's three. An analysis of a clean control that gave no
    answer counts as an alarm: it has not shown the change to be quiet."""
    summary = heldout["summary"]
    model, by_case = summary["model"], summary["by_case"]
    wrong = []
    if heldout.get("set") != "heldout" or heldout.get("dry_run"):
        wrong.append("not a live run of the held-out set")
    if len(heldout["cases"]) != ANALYST_CHANGES or heldout.get("reps") != ANALYST_REPS:
        wrong.append("not 14 changes analyzed twice")
    if heldout.get("model") != PINNED_MODEL:
        wrong.append(f"the model is {heldout.get('model')}")
    dev_ok = (
        dev is not None
        and dev.get("set") == "dev"
        and not dev.get("dry_run")
        and dev.get("model") == heldout.get("model")
        and dev["summary"]["clean"] == DEV_CLEAN
    )
    ok = not wrong
    expected = summary["analyses_expected"]
    complete = summary["analyses_run"] == expected
    called = summary["analyses_run"] - model["not_called"]
    answered = model["answered"]["n"]
    flagged, by_rules = by_case["model_flagged"], by_case["rules_flagged"]
    alarmed = by_case["model_alarmed"]
    dev_alarmed = dev["summary"]["by_case"]["model_alarmed"] if dev_ok and dev else None
    found = [
        criterion(
            "C1, called",
            (called == expected) if ok else None,
            f"{called} of {expected} analyses sent a request",
            "every one",
        ),
        criterion(
            "C2, finishes",
            (answered >= C2_ANSWERED) if ok and complete else None,
            f"{answered} of {expected} analyses answered",
            f"at least {C2_ANSWERED} of {ANALYST_CHANGES * ANALYST_REPS}",
        ),
        criterion(
            "C3, flags the faulty statement",
            (flagged >= C3_FLAGGED and flagged > by_rules) if ok and complete else None,
            f"by case, {flagged} of {summary['faults']} faults flagged at the faulty "
            f"statement; the rules flag {by_rules}",
            f"at least {C3_FLAGGED} of 11, and more than the rules",
        ),
        criterion(
            "C4, quiet on correct changes",
            (alarmed <= C4_HELDOUT_ALARMS and dev_alarmed <= C4_DEV_ALARMS)
            if ok and complete and dev_alarmed is not None
            else None,
            f"alarmed on {alarmed} of {summary['clean']} held-out clean controls (the rules on "
            f"{by_case['rules_alarmed']}), and on {dev_alarmed} of {DEV_CLEAN} development ones"
            + ("" if dev_ok else " (no live development stage of the same model was given)"),
            f"at most {C4_HELDOUT_ALARMS} of 3 held-out and {C4_DEV_ALARMS} of {DEV_CLEAN} "
            "development",
        ),
    ]
    met = {entry["criterion"][:2]: entry["met"] for entry in found}
    result = ("none: " + "; ".join(wrong)) if wrong else outcome(met, ("C2",), "C1")
    return {"role": "Change Analyst", "criteria": found, "outcome": result}


def verdict_markdown(found: Mapping[str, Any]) -> str:
    words = {True: "met", False: "**not met**", None: "undecided"}
    lines = [
        f"# AI evaluation v2: the {found['role']}'s acceptance criteria",
        "",
        "Generated by `python -m dataproof_benchmark eval2-verdict`. Do not edit by hand.",
        "",
        "| Criterion | Needs | Measured | |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {entry['criterion']} | {entry['needs']} | {entry['value']} | {words[entry['met']]} |"
        for entry in found["criteria"]
    ]
    lines += ["", f"**Outcome: {found['outcome']}.**", ""]
    return "\n".join(lines)
