"""The AI ablation's frozen scoring rules and its guard on live runs; no database, no model."""

from decimal import Decimal

import pytest

from dataproof_benchmark import ablation
from dataproof_benchmark.__main__ import CORPUS
from dataproof_benchmark.files import labels


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
