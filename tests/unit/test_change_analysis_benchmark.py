"""The change-analysis benchmark: committed results that reproduce, and a model arm that only
runs with a scripted model here (no model is called)."""

import json
from decimal import Decimal

from dataproof_benchmark import change_analysis
from dataproof_benchmark.__main__ import CHANGE_REPORT, CHANGE_RESULTS, change_results
from dataproof_investigator import RULES


def test_the_committed_results_are_what_the_rules_give_now() -> None:
    committed = json.loads(CHANGE_RESULTS.read_text("utf-8"))
    assert change_results(dry_run=False, live=False, budget_usd=None) == committed


def test_the_committed_report_is_generated_from_the_committed_results() -> None:
    committed = json.loads(CHANGE_RESULTS.read_text("utf-8"))
    assert CHANGE_REPORT.read_text("utf-8") == change_analysis.report(committed)


def test_every_rule_names_the_families_it_anticipates() -> None:
    assert {rule.rule_id for rule in RULES} == set(change_analysis.RULE_FAMILIES)


def test_a_dry_run_analyzes_every_case_and_cites_only_its_reads() -> None:
    document = change_results(dry_run=True, live=False, budget_usd=None)
    summary = document["summary"]
    assert document["dry_run"] is True
    assert summary["model"]["analyzed"] == summary["faults"] + summary["clean_controls"] == 34
    assert summary["model"]["valid_citations"] == summary["model"]["citations"] == 34
    assert summary["model"]["cost_usd"] == "0"
    assert summary["rules"] == json.loads(CHANGE_RESULTS.read_text("utf-8"))["summary"]["rules"]
    assert "not a result" in change_analysis.report(document)


def test_the_worst_case_fits_the_largest_budget() -> None:
    cases = 34
    assert change_analysis.CASE_CAP_USD * cases <= Decimal(10)
