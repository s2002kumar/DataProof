"""The benchmark corpus is frozen, sized as BENCHMARK asks, and leaks no labels (#12)."""

import json
import re

from dataproof_benchmark.__main__ import CORPUS, REPORT, RESULTS
from dataproof_benchmark.corpus import build_api_cases, build_corpus
from dataproof_benchmark.files import stale
from dataproof_benchmark.score import report
from dataproof_runner.project import ProjectConfig

#: Words that would tell a reader what a case is.
REVEALING = re.compile(
    r"multipl|transpos|duplicat|lossy|shift|confusion|orphan|partial|retry|wrong|corrupt|"
    r"holdout|\b(faults?|clean|bugs?|labels?|expected)\b",
    re.IGNORECASE,
)


def test_the_committed_corpus_is_what_the_frozen_seed_generates() -> None:
    assert stale(CORPUS) == [], "regenerate: python -m dataproof_benchmark corpus"


def test_the_corpus_has_the_cases_benchmark_asks_for() -> None:
    cases = build_corpus()
    faults = [c for c in cases if c.label.kind == "fault"]
    cleans = [c for c in cases if c.label.kind == "clean"]
    assert len(faults) >= 20
    assert len(cleans) >= 10
    assert len({c.label.family for c in faults}) >= 10
    assert any(c.holdout for c in faults)
    assert any(c.holdout for c in cleans)
    assert len({c.case_id for c in cases}) == len({c.revision for c in cases}) == len(cases)


def test_nothing_a_reviewer_or_investigator_reads_names_the_fault() -> None:
    for case in [*build_corpus(), *build_api_cases()]:
        assert re.fullmatch(r"case-\d+", case.case_id)
        assert re.fullmatch(r"ledger-backfill-\d+", case.change)
        assert not REVEALING.search(case.source()), case.case_id
        assert not REVEALING.search(case.api_source or ""), case.case_id
    for path in [*(CORPUS / "changes").rglob("*"), *(CORPUS / "api").rglob("*")]:
        if path.is_file() and "__pycache__" not in path.parts:
            assert not REVEALING.search(path.read_text("utf-8")), path


def test_labels_are_only_in_the_evaluator_manifest_outside_every_source_root() -> None:
    (manifest,) = [p for p in CORPUS.rglob("*") if p.is_file() and p.suffix == ".json"]
    assert manifest.name == "labels.json"
    for root in ProjectConfig.model_fields["investigator_source_roots"].default:
        assert not root.startswith(("benchmarks/", "docs/", "dataproof/"))
    assert json.loads(manifest.read_text("utf-8"))["provenance"].endswith(
        "not independently authored."
    )


def test_the_committed_report_is_generated_from_the_committed_results() -> None:
    results = json.loads(RESULTS.read_text("utf-8"))
    assert REPORT.read_text("utf-8") == report(results)


def test_the_api_cases_are_two_faults_and_a_clean_control_with_their_own_api() -> None:
    cases = build_api_cases()
    assert [c.label.kind for c in sorted(cases, key=lambda c: c.case_id)].count("fault") == 2
    assert sum(c.label.kind == "clean" for c in cases) == 1
    for case in cases:
        assert case.api_source is not None
        compile(case.api_source, case.case_id, "exec")
        assert set(case.label.failing) <= {
            "commerce.c6_order_totals_api",
            "commerce.c6_refund_events_api",
        }
