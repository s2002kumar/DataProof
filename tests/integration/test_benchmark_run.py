"""A fresh run of the benchmark reproduces the committed results exactly."""

import json

import pytest

from dataproof_benchmark.__main__ import CORPUS, RESULTS, results_for

pytestmark = pytest.mark.integration


def test_rerunning_the_benchmark_reproduces_the_committed_results(postgres_dsn: str) -> None:
    fresh = json.loads(json.dumps(results_for(postgres_dsn, CORPUS)))
    committed = json.loads(RESULTS.read_text("utf-8"))
    assert fresh == committed, "rerun: python -m dataproof_benchmark run --database <admin DSN>"
