"""The second evaluation's acceptance criteria, as code (docs/AI_EVALUATION_V2.md).

Written before any live run, so that what a result means is not decided after seeing it. A
criterion is met, not met or undecided. It is undecided when what it needs was not measured
(a stage that is not the one specified, a mark the owner has not given), and never met by
default: a criterion that passes on nothing is how the first evaluation reported a stage in
which no model was called.

Two graders decide. What a machine can decide, it decides: whether an investigation answered,
whether its answer states lines within the faulty statement, what it cost. Whether an answer
names the cause is the owner's reading, blind to which arm wrote it (``sheet``): the keyword
proxy passes a hedge and fails a correct paraphrase, and a review showed both.
"""

import csv
import io
import random
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

HELDOUT_FAULTS = 11
HELDOUT_REPS = 3
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
C2_ANSWERED = 25
C3_FLAGGED = 6
C4_HELDOUT_ALARMS = 1
C4_DEV_ALARMS = 3

SHEET_SEED = 20261120
MARKS = ("cause", "statement", "actionable", "grounded")
"""What the owner marks on each answer, y or n: does it name the cause the reference gives;
does it point at the faulty statement; would it have told me what to fix; does it state only
what its evidence shows."""


def criterion(name: str, met: bool | None, value: str, needs: str) -> dict[str, Any]:
    return {"criterion": name, "met": met, "value": value, "needs": needs}


# The blinded sheet -------------------------------------------------------------------------------


def sheet(
    arms: Mapping[str, Mapping[str, Any]],
    references: Mapping[str, Mapping[str, Any]],
    seed: int = SHEET_SEED,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Every answer to be read, in a shuffled order, without the arm that wrote it; and the
    key that says which is which, to be opened only when the marks are in.

    ``arms`` is each model arm's results document by its name ("improved", "old"). The rules'
    diagnosis of each case is an item too, from the first document, so that "the rules miss
    it" is the same reader's judgment and not a keyword's.
    """
    entries: list[tuple[str, str, int | None, Mapping[str, Any]]] = []
    for position, (arm, document) in enumerate(arms.items()):
        for case in document["cases"]:
            if case["rules"] is None or case["kind"] != "fault":
                continue
            if position == 0 and case["rules"]["answer"]:
                entries.append(("rules", case["case_id"], None, case["rules"]["answer"]))
            entries += [
                (arm, case["case_id"], run["rep"], run["answer"])
                for run in case["runs"]
                if run["answer"]
            ]
    random.Random(seed).shuffle(entries)  # noqa: S311 - an order to read in, not a secret
    items, key = [], {}
    for number, (arm, case_id, rep, answer) in enumerate(entries, 1):
        name = f"item-{number:03d}"
        items.append({"item": name, "reference": references[case_id], "answer": answer})
        key[name] = {"arm": arm, "case_id": case_id, "rep": rep}
    return items, key


def sheet_markdown(items: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "# Answers to mark",
        "",
        "Each item is one answer about one migration. The reference says what is actually "
        "wrong. Which arm wrote an answer is not shown. In `marks.csv`, give each item four "
        "marks, `y` or `n`:",
        "",
        "- **cause:** the answer names the cause the reference gives, in its first hypothesis.",
        "- **statement:** it points at the faulty statement.",
        "- **actionable:** it would have told you what to fix.",
        "- **grounded:** it states nothing its evidence could not show.",
        "",
    ]
    for item in items:
        reference, answer = item["reference"], item["answer"]
        lines += [
            f"## {item['item']}",
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
        ]
        for number, hypothesis in enumerate(answer["hypotheses"], 1):
            lines.append(f"{number}. ({hypothesis['confidence']}) {hypothesis['statement']}")
        lines += [
            "",
            f"Suggested action: {answer.get('suggested_action') or 'none'}",
            "",
            f"Caveat: {answer.get('caveat') or 'none'}",
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
    """The owner's marks by item. Anything but y or n is no mark."""
    marks: dict[str, dict[str, bool | None]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        marks[row["item"]] = {
            name: {"y": True, "n": False}.get((row.get(name) or "").strip().lower())
            for name in MARKS
        }
    return marks


# The Failure Investigator ------------------------------------------------------------------------


def _marked(
    marks: Mapping[str, Mapping[str, bool | None]],
    key: Mapping[str, Mapping[str, Any]],
    arm: str,
) -> dict[tuple[str, int | None], Mapping[str, bool | None]]:
    return {
        (entry["case_id"], entry["rep"]): marks.get(item, {})
        for item, entry in key.items()
        if entry["arm"] == arm
    }


def verdict(
    document: Mapping[str, Any],
    marks: Mapping[str, Mapping[str, bool | None]] | None = None,
    key: Mapping[str, Mapping[str, Any]] | None = None,
    arm: str = "improved",
) -> dict[str, Any]:
    """A1 to A7 for a held-out investigation stage, and what they mean for the product."""
    summary = document["summary"]
    model = summary["model"]
    faults = [c for c in document["cases"] if c["rules"] is not None]
    reps = document["reps"]
    specified = (
        document["set"] == "heldout"
        and len(faults) == HELDOUT_FAULTS
        and reps == HELDOUT_REPS
        and not document["dry_run"]
    )
    expected = summary["investigations_expected"]
    answered = model["answered"]["n"]
    found: list[dict[str, Any]] = []

    found.append(
        criterion(
            "A1, finishes",
            (answered >= A1_ANSWERED) if specified else None,
            f"{answered} of {expected} investigations answered ({summary['investigations_run']} "
            "ran)",
            f"at least {A1_ANSWERED} of {HELDOUT_FAULTS * HELDOUT_REPS}",
        )
    )
    stated, unsupported = model["specifics"], model["unsupported_specifics"]
    found.append(
        criterion(
            "A2, grounded",
            (Decimal(unsupported) / stated <= A2_UNSUPPORTED) if specified and stated else None,
            f"{unsupported} of {stated} stated specifics unsupported by the evidence cited",
            "at most 5% (undecided if none was stated)",
        )
    )

    # A3: by case, the owner's blinded marks. A repeat that gave no answer names nothing.
    named = beyond = rules_only = None
    marked_all = False
    actionable_of = actionable = 0
    if marks is not None and key is not None:
        ours, rules = _marked(marks, key, arm), _marked(marks, key, "rules")
        owed = [(c["case_id"], r["rep"]) for c in faults for r in c["runs"] if r["answer"]]
        owed_rules = [(c["case_id"], None) for c in faults if c["rules"]["answer"]]
        marked_all = all(ours.get(k, {}).get("cause") is not None for k in owed) and all(
            rules.get(k, {}).get("cause") is not None for k in owed_rules
        )
        if marked_all:
            named = beyond = rules_only = 0
            for case in faults:
                yes = sum(
                    bool(ours.get((case["case_id"], run["rep"]), {}).get("cause"))
                    for run in case["runs"]
                )
                by_model = yes * 2 > reps
                by_rules = bool(rules.get((case["case_id"], None), {}).get("cause"))
                named += by_model
                beyond += by_model and not by_rules
                rules_only += by_rules and not by_model
            causes = [m for k, m in ours.items() if k in owed and m.get("cause")]
            actionable_of = len(causes)
            actionable = sum(bool(m.get("actionable")) for m in causes)
    a3 = None
    if specified and named is not None and beyond is not None and rules_only is not None:
        a3 = named >= A3_NAMED and beyond >= A3_BEYOND_RULES and rules_only <= A3_RULES_ONLY
    by_case = summary["by_case"]
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
            + f". The keyword proxy: {by_case['model_named_root_cause']} named, "
            f"{by_case['model_only']} beyond the rules, {by_case['rules_only']} rules only",
            f"at least {A3_NAMED} of {HELDOUT_FAULTS}, at least {A3_BEYOND_RULES} of them "
            f"missed by the rules, at most {A3_RULES_ONLY} where only the rules name it",
        )
    )
    located = by_case["model_located"]
    found.append(
        criterion(
            "A4, locates",
            (located >= A4_LOCATED) if specified else None,
            f"by case, the faulty statement's lines stated in {located} of {len(faults)}",
            f"at least {A4_LOCATED} of {HELDOUT_FAULTS}",
        )
    )
    cost = Decimal(model["cost_usd_median"]) if model["cost_usd_median"] else None
    seconds = model["latency_ms_median"] / 1000 if model["latency_ms_median"] is not None else None
    found.append(
        criterion(
            "A5, affordable",
            (cost <= A5_COST_USD and seconds <= A5_SECONDS)
            if specified and cost is not None and seconds is not None
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
            (not problems and not unsent) if specified else None,
            f"{len(problems)} plumbing faults; {len(unsent)} investigations sent no request",
            "none of either, on a live run of the held-out stage as specified",
        )
    )
    found.append(
        criterion(
            "A7, the owner finds it actionable",
            (Decimal(actionable) / actionable_of >= A7_ACTIONABLE)
            if specified and marked_all and actionable_of
            else None,
            f"{actionable} of the {actionable_of} answers marked as naming the cause are "
            "marked actionable"
            if marked_all
            else "the owner's marks are not in",
            "at least 4 in 5",
        )
    )
    met = {entry["criterion"][:2]: entry["met"] for entry in found}
    if not specified:
        outcome = "none: this is not a live run of the held-out stage as specified"
    elif met["A6"] is False:
        outcome = "void: the stage is not a measurement"
    elif met["A1"] is False or met["A2"] is False:
        outcome = "disabled"
    elif None in (met["A3"], met["A7"]):
        outcome = "undecided until the owner's marks are in"
    elif all(met.values()):
        outcome = "optional"
    else:
        outcome = "experimental"
    return {"role": "Failure Investigator", "criteria": found, "outcome": outcome}


# The Change Analyst ------------------------------------------------------------------------------


def analysis_verdict(
    heldout: Mapping[str, Any], dev: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The Change Analyst's criteria on the held-out stage, with the development set's clean
    controls beside the held-out set's three."""
    summary = heldout["summary"]
    model, by_case = summary["model"], summary["by_case"]
    specified = (
        heldout["set"] == "heldout"
        and len(heldout["cases"]) == ANALYST_CHANGES
        and heldout["reps"] == ANALYST_REPS
        and not heldout["dry_run"]
    )
    expected = summary["analyses_expected"]
    called = summary["analyses_run"] - model["not_called"]
    answered = model["answered"]["n"]
    flagged, by_rules = by_case["model_flagged"], by_case["rules_flagged"]
    alarmed = by_case["model_alarmed"]
    dev_alarmed = dev["summary"]["by_case"]["model_alarmed"] if dev else None
    dev_clean = dev["summary"]["clean"] if dev else None
    found = [
        criterion(
            "C1, called",
            (called == expected) if specified else None,
            f"{called} of {expected} analyses sent a request",
            "every one",
        ),
        criterion(
            "C2, finishes",
            (answered >= C2_ANSWERED) if specified else None,
            f"{answered} of {expected} analyses answered",
            f"at least {C2_ANSWERED} of {ANALYST_CHANGES * ANALYST_REPS}",
        ),
        criterion(
            "C3, flags the faulty statement",
            (flagged >= C3_FLAGGED and flagged > by_rules) if specified else None,
            f"by case, {flagged} of {summary['faults']} faults flagged at the faulty "
            f"statement; the rules flag {by_rules}",
            f"at least {C3_FLAGGED} of 11, and more than the rules",
        ),
        criterion(
            "C4, quiet on correct changes",
            (alarmed <= C4_HELDOUT_ALARMS and dev_alarmed <= C4_DEV_ALARMS)
            if specified and dev_alarmed is not None
            else None,
            f"alarmed on {alarmed} of {summary['clean']} held-out clean controls (the rules on "
            f"{by_case['rules_alarmed']}), and on {dev_alarmed} of {dev_clean} development ones",
            f"at most {C4_HELDOUT_ALARMS} of 3 held-out and {C4_DEV_ALARMS} of 11 development "
            "(undecided without the development stage)",
        ),
    ]
    met = {entry["criterion"][:2]: entry["met"] for entry in found}
    if not specified:
        outcome = "none: this is not a live run of the held-out stage as specified"
    elif met["C1"] is False:
        outcome = "void: the stage is not a measurement"
    elif met["C2"] is False:
        outcome = "disabled"
    elif all(met.values()):
        outcome = "optional"
    elif None in met.values():
        outcome = "undecided until the development stage has run"
    else:
        outcome = "experimental"
    return {"role": "Change Analyst", "criteria": found, "outcome": outcome}


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
