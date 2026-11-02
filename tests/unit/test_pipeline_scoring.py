"""Pipeline-tier scoring: each label is held to the contracts it was written for."""

from dataproof_benchmark.__main__ import pipeline_cases, pipeline_score
from dataproof_benchmark.pipeline_tier import ALL_CONTRACTS, DATA_CONTRACTS

C1, C2, C3, C6_TOTALS, C6_REFUNDS = ALL_CONTRACTS


def _result(**outcomes: str) -> dict[str, object]:
    recorded = dict.fromkeys(ALL_CONTRACTS, "PASS")
    recorded.update({f"commerce.{name}": outcome for name, outcome in outcomes.items()})
    return {"verdict": "BLOCKED", "outcomes": recorded}


def test_a_data_fault_seen_through_the_api_too_is_reported_not_scored() -> None:
    (case,) = pipeline_cases(["case-104"])  # join multiplication: C1 to C3 labelled
    result = {
        **_result(
            c1_event_preservation="FAIL",
            c2_refund_totals="FAIL",
            c3_capture_totals="FAIL",
            c6_order_totals_api="FAIL",
        ),
        "expected_failing": sorted(case.label.failing),
    }
    pipeline_score(case, result)
    assert result["labelled_contracts"] == list(DATA_CONTRACTS)
    assert result["agrees"] is True
    assert result["also_failed"] == [C6_TOTALS]


def test_a_data_fault_missing_a_labelled_contract_disagrees() -> None:
    (case,) = pipeline_cases(["case-104"])
    result = {
        **_result(c1_event_preservation="FAIL"),
        "expected_failing": sorted(case.label.failing),
    }
    pipeline_score(case, result)
    assert result["agrees"] is False


def test_api_cases_are_scored_on_every_contract() -> None:
    (case,) = pipeline_cases(["case-201"])
    result = {**_result(c6_order_totals_api="FAIL"), "expected_failing": sorted(case.label.failing)}
    pipeline_score(case, result)
    assert result["labelled_contracts"] == list(ALL_CONTRACTS)
    assert (result["agrees"], result["also_failed"]) == (True, [])
    result = {**_result(c6_order_totals_api="FAIL", c2_refund_totals="FAIL")}
    result["expected_failing"] = sorted(case.label.failing)
    pipeline_score(case, result)
    assert result["agrees"] is False, "a data contract failing on an API case is a disagreement"
