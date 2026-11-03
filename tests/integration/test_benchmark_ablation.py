"""The AI ablation harness on real PostgreSQL, with scripted models only: no model is called.

It checks the plumbing a live run depends on: the same evidence for both arms, a read-only
investigator, the accounting, and a budget that stops before it can be overspent.
"""

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

import psycopg
import pytest

from dataproof_benchmark import ablation
from dataproof_benchmark.__main__ import CORPUS
from dataproof_benchmark.files import labels
from dataproof_benchmark.harness import databases
from dataproof_contracts import CheckResult
from dataproof_investigator import InvestigatorModel, ScriptedModel, call

pytestmark = pytest.mark.integration

JOIN, RETRY, CLEAN, DUPLICATED = "case-104", "case-105", "case-101", "case-107"


def test_a_dry_run_diagnoses_each_failing_case_in_both_arms(postgres_dsn: str) -> None:
    rows = ablation.investigate(
        postgres_dsn, CORPUS, ablation.dry_run_model, Decimal(10), [CLEAN, JOIN, DUPLICATED]
    )
    document = ablation.results(
        rows, labels(CORPUS), live=False, budget_usd=Decimal(10), model="scripted:dry-run"
    )
    cases = {c["case_id"]: c for c in document["cases"]}

    assert cases[CLEAN]["skipped"] == "no contract failed or errored"
    assert (cases[CLEAN]["model_off"], cases[CLEAN]["model"]) == (None, None)

    join = cases[JOIN]
    assert join["skipped"] is None
    assert "commerce.c1_event_preservation" in join["failed"]
    assert join["model_off"]["status"] == "COMPLETED"
    assert join["model_off"]["root_cause"] is True, "the rules name the multiplying join"
    assert join["model_off"]["cost_usd"] == "0"
    model = join["model"]
    assert model["status"] == "COMPLETED", model["failure"]
    assert model["model"] == "scripted:dry-run"
    assert model["tool_calls"] == {"OK": 1}
    duplicated = cases[DUPLICATED]
    assert duplicated["failed"] == ["commerce.c1_event_preservation"], "an errored check counts"
    assert duplicated["model"]["status"] == "COMPLETED", duplicated["model"]["failure"]
    assert document["dry_run"] is True
    assert document["summary"]["model"]["investigations"] == 2
    assert "not results" in ablation.report(document)

    # The label-free measures, from what each arm actually said and cited.
    assert (model["citations"], model["valid_citations"]) == (1, 1)
    assert model["unsupported_specifics"] == [], "it names only the check its tool call read"
    rules = join["model_off"]
    assert rules["valid_citations"] == rules["citations"] > 0
    assert rules["unsupported_specifics"] == []
    assert rules["latency_ms"] >= 0, "measured, not assumed"
    kept = ablation.transcripts(rows)
    assert set(kept) == {JOIN, DUPLICATED}
    asked, answered = kept[JOIN]
    assert asked["tool_uses"] == [
        {"name": "get_check_result", "arguments": {"check_id": "commerce.c1_event_preservation"}}
    ]
    (result,) = answered["tool_results"]
    assert result["sha256"]
    assert not result["is_error"]
    assert answered["stop"] == "end"


def test_the_budget_stops_before_a_case_could_overspend_it(postgres_dsn: str) -> None:
    def costly(checks: Mapping[str, CheckResult]) -> InvestigatorModel:
        first = ablation.failed(checks)[0]
        turn = replace(call("get_check_result", {"check_id": first}), cost_usd=Decimal("0.20"))
        return ScriptedModel([turn] * 20, turn_ceiling_usd=Decimal("0.20"))

    # 0.60 covers the first case's 0.50 cap; after its 0.40, 0.20 is left and the second waits.
    rows = ablation.investigate(postgres_dsn, CORPUS, costly, Decimal("0.60"), [JOIN, RETRY])
    first, second = rows

    assert first["model"]["status"] == "BUDGET_EXHAUSTED"
    assert first["model_measures"]["answer_parsed"] is False, "it never answered"
    assert Decimal(first["model"]["usage"]["cost_usd"]) == Decimal("0.40"), (
        "a third turn could have reached 0.60, past the 0.50 cap, so it was never sent"
    )
    assert second["model"] is None
    assert second["model_off"] is not None, "the model-off arm still runs"
    assert second["skipped"] == "the budget left cannot cover the per-case cap"


def test_the_investigator_connects_as_a_role_that_cannot_write(postgres_dsn: str) -> None:
    with ablation.reader(postgres_dsn) as as_reader, databases(postgres_dsn) as new_database:
        dsn = new_database()
        with psycopg.connect(as_reader(dsn)) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TABLE written (x int)")
