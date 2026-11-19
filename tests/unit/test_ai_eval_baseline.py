"""The first live AI evaluation (ai-eval-v1): its outputs stay exactly as the run wrote them,
and the committed audit is what the code gives now. No model and no database is called."""

import json
import shutil
from collections import Counter
from decimal import Decimal
from pathlib import Path

from dataproof_benchmark import ablation, audit
from dataproof_benchmark.__main__ import ABLATION_REPORT, ABLATION_RESULTS
from dataproof_investigator.anthropic_model import CACHE_WRITE, PRICES


def manifest() -> dict[str, object]:
    document: dict[str, object] = json.loads((audit.RESULTS / audit.MANIFEST).read_text("utf-8"))
    return document


def test_the_frozen_outputs_match_their_manifest() -> None:
    files = manifest()["files"]
    assert isinstance(files, dict)
    assert len(files) == 23  # three result files, two pilot and eighteen main transcripts
    assert audit.digests() == files
    assert audit.changed() == []


def test_a_changed_added_or_missing_frozen_file_is_reported(tmp_path: Path) -> None:
    copy = tmp_path / "results"
    copy.mkdir()
    shutil.copy(audit.RESULTS / audit.MANIFEST, copy)
    for path in audit.frozen_files():
        target = copy / path.relative_to(audit.RESULTS)
        target.parent.mkdir(exist_ok=True)
        shutil.copy(path, target)
    assert audit.changed(copy) == []

    edited = copy / "ablation-transcripts" / "case-104.json"
    edited.write_text(edited.read_text("utf-8").replace("BUDGET", "budget", 1) + " ", "utf-8")
    (copy / "ablation-transcripts" / "case-121.json").unlink()
    (copy / "ablation-transcripts" / "case-999.json").write_text("[]", "utf-8")
    assert audit.changed(copy) == [
        "ablation-transcripts/case-104.json",
        "ablation-transcripts/case-121.json",
        "ablation-transcripts/case-999.json",
    ]


def test_nothing_unpublishable_is_in_the_frozen_outputs() -> None:
    assert audit.forbidden_content() == []
    for leaked in (
        "sk-ant-api03-abc",
        json.dumps("C:\\Users\\someone\\repo"),
        "/home/someone/repo",
        "postgresql://reader:secret@db/postgres",
        "host=127.0.0.1 password=x",
    ):
        assert audit.FORBIDDEN.search(leaked), leaked
    assert not audit.FORBIDDEN.search(json.dumps({"sql": "SELECT order_id FROM ledger_entries"}))


def test_the_committed_reports_are_generated_from_the_frozen_outputs() -> None:
    data = audit.load()
    generated = audit.report(data, unchanged=True, clean=True)
    assert (audit.RESULTS / audit.AUDIT_REPORT).read_text("utf-8") == generated
    assert (audit.RESULTS / audit.TRACES_REPORT).read_text("utf-8") == audit.traces(data)
    # The ablation report is the frozen harness's own, unchanged since before the run.
    frozen = json.loads(ABLATION_RESULTS.read_text("utf-8"))
    assert ABLATION_REPORT.read_text("utf-8") == ablation.report(frozen)


def test_the_audit_prices_are_the_adapters() -> None:
    input_price, output_price, read_price = PRICES["claude-opus-5-5"]
    assert input_price == audit.INPUT_PRICE
    assert output_price == audit.OUTPUT_PRICE
    assert read_price == audit.READ_PRICE
    assert input_price * CACHE_WRITE == audit.WRITE_PRICE


def test_the_spend_reconciles_turn_by_turn_and_token_by_token() -> None:
    data = audit.load()
    stages = [audit.stage_audit(stage) for stage in data["stages"]]
    for stage in stages:
        assert (
            stage["turn_costs"]
            == stage["case_costs"]
            == stage["summary_cost"]
            == stage["recomputed"]
        )
    change = audit.change_audit(data["change"])
    recorded = manifest()["recorded_cost_usd"]
    assert isinstance(recorded, dict)
    assert [stage["turn_costs"] for stage in stages] == [
        Decimal(recorded["pilot"]),
        Decimal(recorded["main"]),
    ]
    assert change["cost"] == Decimal(recorded["change_analyst"]) == 0
    total = sum((stage["turn_costs"] for stage in stages), Decimal(0)) + change["cost"]
    assert total == Decimal(recorded["total"]) == Decimal("1.651932")


def test_what_the_main_run_showed() -> None:
    cases = audit.stage_audit(audit.load()["stages"][1])["cases"]
    assert Counter(c["status"] for c in cases) == {"BUDGET_EXHAUSTED": 17, "COMPLETED": 1}
    stopped = [c for c in cases if c["status"] == "BUDGET_EXHAUSTED"]
    # Every stopped investigation was stopped by the tool-call limit, never by cost or turns.
    assert {c["failure"] for c in stopped} == {"more than 12 tool calls"}
    assert all(c["run"] == 12 for c in cases)
    assert (
        sum(c["requested"] for c in cases),
        sum(c["run"] for c in cases),
        sum(c["read"] for c in cases),
    ) == (240, 216, 205)
    # No investigation ever read a line of the migration it was explaining.
    assert sum(c["source_attempts"] for c in cases) == 34
    assert sum(c["source_ok"] for c in cases) == 0
    assert sum(c["recorded_fetches"] for c in cases) == 88
    assert sum(c["repeats"] for c in cases) == 0
    assert Counter(c["named_by"] for c in cases) == {
        "rules only": 9,
        "neither": 8,
        "model only": 1,
    }


def test_the_change_analyst_stage_called_no_model() -> None:
    change = audit.change_audit(audit.load()["change"])
    assert change["statuses"] == {"BUDGET_EXHAUSTED": 34}
    assert change["turns"] == 0
    assert change["cost"] == 0
    # One turn's worst case was above the cap for a whole analysis, so none was sent.
    assert change["ceilings"] == [Decimal("0.194055")]
    assert change["ceilings"][0] > change["cap"] == Decimal("0.15")


def test_each_call_is_paired_with_what_became_of_it() -> None:
    def use(name: str) -> dict[str, object]:
        return {"name": name, "arguments": {"n": name}}

    def result(content: object, is_error: bool = False) -> dict[str, object]:
        return {"content": json.dumps(content), "is_error": is_error}

    turns = [
        {"tool_results": [], "tool_uses": [use("a"), use("b"), use("c")]},
        {
            "tool_results": [
                result({"evidence_id": "investigation.tool.1"}),
                result({"denied": "no"}, is_error=True),
                result({"failed": "broke"}, is_error=True),
            ],
            "tool_uses": [use("d"), use("e"), use("f")],
        },
    ]
    found = audit.calls(turns, run=4)
    assert [(c["tool"], c["turn"], c["state"], c["detail"]) for c in found] == [
        ("a", 1, "OK", ""),
        ("b", 1, "DENIED", "no"),
        ("c", 1, "FAILED", "broke"),
        ("d", 2, audit.UNREAD, ""),
        ("e", 2, audit.NOT_RUN, ""),
        ("f", 2, audit.NOT_RUN, ""),
    ]
