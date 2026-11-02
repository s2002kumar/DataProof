"""The pipeline tier, smoke-tested in CI: one API behaviour fault through the real worker.

The full tier runs locally (``python -m dataproof_benchmark pipeline``); this runs its first
API case end to end on Docker, with previews, and checks it against the committed results.
"""

import json
from pathlib import Path

import pytest

from dataproof_benchmark.__main__ import PIPELINE_RESULTS, pipeline_cases, pipeline_score
from dataproof_benchmark.pipeline_tier import run

pytestmark = [pytest.mark.integration, pytest.mark.docker]
CASE = "case-201"


def test_an_api_fault_is_caught_through_the_previews_and_nothing_is_left_behind(
    postgres_dsn: str, runtime_image: str, tmp_path: Path
) -> None:
    (case,) = pipeline_cases([CASE])
    (result,) = run(postgres_dsn, runtime_image, [case], tmp_path / "work")
    pipeline_score(case, result)

    assert result["state_at_review"] == "READY_FOR_REVIEW"
    assert result["final_state"] == "COMPLETE"
    assert result["left_behind"] == 0, "cleanup removed every container, network and volume"
    outcomes = result["outcomes"]
    assert outcomes["migration.execute"] == "PASS", "the migration itself is correct"
    assert outcomes["preview.candidate"] == "PASS"
    for contract in (
        "commerce.c1_event_preservation",
        "commerce.c2_refund_totals",
        "commerce.c3_capture_totals",
    ):
        assert outcomes[contract] == "PASS", "the data is right; only the API is wrong"
    assert outcomes["commerce.c6_order_totals_api"] == "FAIL"
    assert result["verdict"] == "BLOCKED"
    assert result["detected"]
    assert result["agrees"]
    assert result["also_failed"] == []
    for stage in ("FETCHING", "PROVISIONING", "MIGRATING", "VERIFYING", "REPORTING", "CLEANING"):
        assert result["stage_seconds"][stage] >= 0
    assert set(result["previews"]) == {"REFERENCE", "CANDIDATE"}

    committed = {c["case_id"]: c for c in json.loads(PIPELINE_RESULTS.read_text("utf-8"))["cases"]}
    assert result["outcomes"] == committed[CASE]["outcomes"], "the committed run, reproduced"
    assert result["verdict"] == committed[CASE]["verdict"]
