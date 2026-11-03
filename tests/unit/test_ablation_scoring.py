"""The AI ablation's frozen scoring rules, its label-free measures and its guard on live runs;
no database, no model."""

import json
import uuid
from decimal import Decimal

import pytest

from dataproof_benchmark import ablation
from dataproof_benchmark.__main__ import CORPUS
from dataproof_benchmark.files import labels
from dataproof_contracts import Investigation
from dataproof_investigator import diagnose


def test_every_fault_family_has_a_rubric() -> None:
    families = {str(c["family"]) for c in labels(CORPUS) if c["kind"] == "fault" and c["failing"]}
    assert families <= set(ablation.RUBRIC)


@pytest.mark.parametrize(
    ("family", "text", "match"),
    [
        ("join multiplication", "Refunds were JOINed to order items before summing.", True),
        ("join multiplication", "Amounts differ for some orders.", False),
        ("balanced loss and duplication", "One event is missing and another duplicated.", True),
        ("balanced loss and duplication", "One event is missing.", False),
        ("timestamp shift", "Instants moved by the UTC offset.", True),
        ("not a family", "anything", None),
    ],
)
def test_root_cause_needs_every_group_of_the_family_rubric(
    family: str, text: str, match: bool | None
) -> None:
    assert ablation.rubric_match(family, [text]) is match


def test_named_events_are_whole_numbers_not_parts_of_amounts() -> None:
    texts = ["Event 1002 refunds 49.99, not 99.98; events 1003 and 17 too."]
    assert ablation.mentioned_numbers(texts) == {1002, 1003, 17}


@pytest.mark.parametrize(
    ("budget", "environ", "message"),
    [
        (None, {"ANTHROPIC_API_KEY": "k"}, "needs --budget-usd"),
        (Decimal("10.01"), {"ANTHROPIC_API_KEY": "k"}, "at most 10"),
        (Decimal(0), {"ANTHROPIC_API_KEY": "k"}, "more than 0"),
        (Decimal(10), {}, "ANTHROPIC_API_KEY"),
    ],
)
def test_a_live_run_needs_an_approved_budget_and_a_key(
    budget: Decimal | None, environ: dict[str, str], message: str
) -> None:
    with pytest.raises(SystemExit, match=message):
        ablation.live_budget(budget, environ)
    assert ablation.live_budget(Decimal(10), {"ANTHROPIC_API_KEY": "k"}) == Decimal(10)


def test_the_worst_case_fits_the_largest_budget() -> None:
    failing = [c for c in labels(CORPUS) if c["kind"] == "fault" and c["failing"]]
    assert ablation.CASE_CAP_USD * len(failing) <= ablation.MAX_BUDGET_USD


def test_specifics_are_ids_amounts_and_identifiers_not_small_numbers() -> None:
    statement = (
        "commerce.c1_event_preservation: event 1002 has 99.98, not 49.99; the join on "
        "order_items repeats it 2 times (investigation.tool.3)."
    )
    assert ablation.specifics(statement) == {
        "c1_event_preservation",
        "1002",
        "99.98",
        "49.99",
        "order_items",
    }


def _rules_only() -> Investigation:
    return diagnose(uuid.uuid4(), {})


TEXTS = {
    "run.evidence": '{"check_id": "commerce.c1_event_preservation", "source_event_id": 1002}',
    "investigation.tool.1": '{"evidence_id": "investigation.tool.1", "amount": "99.980000"}',
}


def test_citations_are_counted_from_the_raw_answer_even_when_it_is_rejected() -> None:
    raw = json.dumps(
        {
            "hypotheses": [
                {
                    "statement": "Event 1002 doubled to 99.98.",
                    "cited_evidence_ids": ["run.evidence", "investigation.tool.1"],
                },
                {"statement": "Order 77777 lost a refund.", "cited_evidence_ids": ["made.up"]},
            ],
            "caveat": "-",
        }
    )
    measured = ablation.measures(_rules_only(), TEXTS, raw)
    assert (measured["citations"], measured["valid_citations"]) == (3, 2)
    assert measured["invalid_citations"] == ["made.up"]
    assert measured["specifics"] == 3
    assert measured["unsupported_specifics"] == ["77777"], "1002 and 99.98 are in what it cites"
    assert measured["hypotheses_with_unsupported"] == 1


def test_a_specific_is_supported_only_by_the_evidence_its_hypothesis_cites() -> None:
    raw = json.dumps(
        {
            "hypotheses": [
                {
                    "statement": "Event 1002 is 99.98.",
                    "cited_evidence_ids": ["investigation.tool.1"],
                }
            ]
        }
    )
    measured = ablation.measures(_rules_only(), TEXTS, raw)
    assert measured["unsupported_specifics"] == ["1002"], "1002 is only in evidence it did not cite"


def test_an_answer_that_is_not_json_has_no_citations() -> None:
    measured = ablation.measures(_rules_only(), TEXTS, "I think it is a join.")
    assert measured == {"answer_parsed": False, "citations": 0, "valid_citations": 0}


def test_spread_is_median_p90_and_max() -> None:
    assert ablation._spread([]) == {"median": None, "p90": None, "max": None}
    spread = ablation._spread([float(v) for v in range(1, 11)])
    assert spread == {"median": 5.5, "p90": 9.0, "max": 10.0}


def test_hedging_is_matching_other_families_rubrics() -> None:
    texts = ["A join multiplied rows; or a time zone offset; or values were rounded."]
    assert ablation.other_rubrics("join multiplication", texts) == 2
