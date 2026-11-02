"""``python -m dataproof_benchmark``: the corpus, a run, and its report.

* ``corpus [--check]``: write (or verify) the frozen corpus from its seed.
* ``run --database <admin DSN>``: run every case at the database tier and score it; writes
  ``benchmarks/results/benchmark-results.json``. The DSN needs to create databases.
* ``report``: write ``benchmarks/results/REPORT.md`` from the results.
"""

import argparse
import json
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from dataproof_benchmark import pipeline_tier
from dataproof_benchmark.corpus import (
    CORPUS_SEED,
    CORPUS_VERSION,
    Case,
    build_api_cases,
    build_corpus,
)
from dataproof_benchmark.evaluate import as_json, run
from dataproof_benchmark.files import PROVENANCE, labels, stale, write
from dataproof_benchmark.score import joined, pipeline_report, report, summarize

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "corpus"
RESULTS = ROOT / "results" / "benchmark-results.json"
REPORT = ROOT / "results" / "REPORT.md"
PIPELINE_RESULTS = ROOT / "results" / "pipeline-results.json"
PIPELINE_REPORT = ROOT / "results" / "PIPELINE.md"
TIER = "database (each case migrated into a fresh PostgreSQL database; no previews)"


def results_for(admin_dsn: str, corpus: Path) -> dict[str, object]:
    """Run, then score: the labels are read only after every case has run."""
    raw = [as_json(result) for result in run(admin_dsn, corpus)]
    cases = joined(raw, labels(corpus))
    return {
        "corpus": {"corpus_version": CORPUS_VERSION, "seed": CORPUS_SEED, "provenance": PROVENANCE},
        "tier": TIER,
        "cases": cases,
        "summary": summarize(cases),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dataproof_benchmark", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    corpus = commands.add_parser("corpus", help="write the corpus from its frozen seed")
    corpus.add_argument("--check", action="store_true", help="fail if the corpus is stale")
    corpus.add_argument("--root", type=Path, default=CORPUS)
    runner = commands.add_parser("run", help="run every case and score it")
    runner.add_argument("--database", required=True, help="an admin DSN that can create databases")
    runner.add_argument("--out", type=Path, default=RESULTS)
    reporter = commands.add_parser("report", help="write the Markdown report")
    reporter.add_argument("--results", type=Path, default=RESULTS)
    reporter.add_argument("--out", type=Path, default=REPORT)
    piped = commands.add_parser("pipeline", help="run the pipeline tier on local Docker")
    piped.add_argument("--database", required=True, help="an admin DSN that can create databases")
    piped.add_argument("--image", required=True, help="the runtime image ID (sha256:...)")
    piped.add_argument("--cases", nargs="*", help="case IDs (default: the tier's sample)")
    piped.add_argument("--workdir", type=Path, default=ROOT / ".work")
    piped.add_argument("--out", type=Path, default=PIPELINE_RESULTS)
    piped_report = commands.add_parser("pipeline-report", help="write the pipeline-tier report")
    piped_report.add_argument("--results", type=Path, default=PIPELINE_RESULTS)
    piped_report.add_argument("--out", type=Path, default=PIPELINE_REPORT)
    args = parser.parse_args(argv)

    if args.command == "corpus":
        if args.check:
            problems = stale(args.root)
            if problems:
                print("stale corpus files:\n  " + "\n  ".join(problems), file=sys.stderr)
                return 1
            return 0
        write(args.root)
    elif args.command == "run":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(results_for(args.database, CORPUS), indent=2) + "\n"
        args.out.write_text(rendered, "utf-8", newline="\n")
    elif args.command == "pipeline":
        args.out.parent.mkdir(parents=True, exist_ok=True)
        measured = pipeline_results(args.database, args.image, args.cases, args.workdir)
        args.out.write_text(json.dumps(measured, indent=2) + "\n", "utf-8", newline="\n")
    elif args.command == "pipeline-report":
        results = json.loads(args.results.read_text("utf-8"))
        args.out.write_text(pipeline_report(results), "utf-8", newline="\n")
    else:
        results = json.loads(args.results.read_text("utf-8"))
        args.out.write_text(report(results), "utf-8", newline="\n")
    return 0


def pipeline_cases(case_ids: Sequence[str] | None) -> list[Case]:
    data, api = build_corpus(), build_api_cases()
    if case_ids:
        by_id = {c.case_id: c for c in [*data, *api]}
        return [by_id[i] for i in case_ids]
    return [*pipeline_tier.sample(data), *api]


def pipeline_score(case: Case, result: dict[str, object]) -> None:
    """Score one run from its recorded outcomes and its case's label."""
    result["labelled_contracts"] = list(pipeline_tier.labelled_contracts(case))
    result["detected"] = pipeline_tier.detected(result)
    result["agrees"] = pipeline_tier.agrees(result)
    result["also_failed"] = pipeline_tier.also_failed(result)


def pipeline_results(
    admin_dsn: str, image: str, case_ids: Sequence[str] | None, workdir: Path
) -> dict[str, object]:
    chosen = pipeline_cases(case_ids)
    cases = pipeline_tier.run(admin_dsn, image, chosen, workdir)
    for case, result in zip(chosen, cases, strict=True):
        pipeline_score(case, result)
    return {
        "tier": "pipeline (the real worker on local Docker, with previews of both commits)",
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "runtime_image": image,
        "cases": cases,
    }


if __name__ == "__main__":
    raise SystemExit(main())
