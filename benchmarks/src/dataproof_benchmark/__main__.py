"""``python -m dataproof_benchmark``: the corpus, a run, and its report.

* ``corpus [--check]``: write (or verify) the frozen corpus from its seed.
* ``run --database <admin DSN>``: run every case at the database tier and score it; writes
  ``benchmarks/results/benchmark-results.json``. The DSN needs to create databases.
* ``report``: write ``benchmarks/results/REPORT.md`` from the results.
* ``pipeline`` and ``pipeline-report``: the pipeline tier on local Docker (``PIPELINE.md``).
* ``ablation --database <admin DSN>``: the AI ablation (B3). Without ``--live`` a scripted model
  runs it at no cost (a dry run, written under ``.work``). ``--live --budget-usd <approved>``
  calls the paid model and needs ``ANTHROPIC_API_KEY``; the protocol is docs/AI_EVALUATION.md.
  Each investigation's transcript is written beside the results. ``ablation-report`` writes
  ``ABLATION.md``.
* ``change-analysis``: the Change Analyst against the static risk rules, before anything runs.
  The rules alone by default; ``--dry-run`` adds a scripted model (written under ``.work``),
  and ``--live --budget-usd <approved>`` the paid one. ``change-analysis-report`` writes
  ``CHANGE_ANALYSIS.md``.
"""

import argparse
import json
import os
import platform
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from dataproof_benchmark import ablation, change_analysis, pipeline_tier
from dataproof_benchmark.corpus import (
    CORPUS_SEED,
    CORPUS_VERSION,
    Case,
    build_api_cases,
    build_corpus,
)
from dataproof_benchmark.evaluate import as_json, run
from dataproof_benchmark.files import PROVENANCE, api_labels, labels, stale, write
from dataproof_benchmark.score import joined, pipeline_report, report, summarize

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "corpus"
RESULTS = ROOT / "results" / "benchmark-results.json"
REPORT = ROOT / "results" / "REPORT.md"
PIPELINE_RESULTS = ROOT / "results" / "pipeline-results.json"
PIPELINE_REPORT = ROOT / "results" / "PIPELINE.md"
ABLATION_RESULTS = ROOT / "results" / "ablation-results.json"
ABLATION_DRY_RUN = ROOT / ".work" / "ablation-dry-run.json"
ABLATION_REPORT = ROOT / "results" / "ABLATION.md"
CHANGE_RESULTS = ROOT / "results" / "change-analysis-results.json"
CHANGE_DRY_RUN = ROOT / ".work" / "change-analysis-dry-run.json"
CHANGE_REPORT = ROOT / "results" / "CHANGE_ANALYSIS.md"
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
    ablate = commands.add_parser("ablation", help="the AI ablation: model-off against the model")
    ablate.add_argument("--database", required=True, help="an admin DSN that can create databases")
    ablate.add_argument("--live", action="store_true", help="call the paid model")
    ablate.add_argument("--budget-usd", type=Decimal, help="the budget approved for a live run")
    ablate.add_argument("--cases", nargs="*", help="case IDs (default: every failing case)")
    ablate.add_argument(
        "--effort",
        choices=("high", "medium"),
        default=ablation.EFFORT,
        help="the model's effort; medium only if the pilot rule calls for it",
    )
    ablate.add_argument("--out", type=Path)
    ablate_report = commands.add_parser("ablation-report", help="write the ablation report")
    ablate_report.add_argument("--results", type=Path, default=ABLATION_RESULTS)
    ablate_report.add_argument("--out", type=Path, default=ABLATION_REPORT)
    change = commands.add_parser(
        "change-analysis", help="the Change Analyst against the static risk rules"
    )
    change.add_argument("--dry-run", action="store_true", help="add a scripted model, at no cost")
    change.add_argument("--live", action="store_true", help="add the paid model")
    change.add_argument("--budget-usd", type=Decimal, help="the budget approved for a live run")
    change.add_argument("--out", type=Path)
    change_report = commands.add_parser(
        "change-analysis-report", help="write the change-analysis report"
    )
    change_report.add_argument("--results", type=Path, default=CHANGE_RESULTS)
    change_report.add_argument("--out", type=Path, default=CHANGE_REPORT)
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
    elif args.command == "ablation":
        document, kept = ablation_results(
            args.database, args.live, args.budget_usd, args.cases, args.effort
        )
        out = args.out or (ABLATION_RESULTS if args.live else ABLATION_DRY_RUN)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(document, indent=2) + "\n", "utf-8", newline="\n")
        folder = out.with_name(out.stem.removesuffix("-results") + "-transcripts")
        folder.mkdir(exist_ok=True)
        for case_id, turns in kept.items():
            text = json.dumps(turns, indent=2, default=str) + "\n"
            (folder / f"{case_id}.json").write_text(text, "utf-8", newline="\n")
        print(json.dumps(document["summary"], indent=2))
        print(f"results: {out}; transcripts: {folder}")
    elif args.command == "change-analysis":
        document = change_results(args.dry_run, args.live, args.budget_usd)
        out = args.out or (CHANGE_DRY_RUN if args.dry_run else CHANGE_RESULTS)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(document, indent=2) + "\n", "utf-8", newline="\n")
        print(json.dumps(document["summary"], indent=2))
    elif args.command == "change-analysis-report":
        results = json.loads(args.results.read_text("utf-8"))
        args.out.write_text(change_analysis.report(results), "utf-8", newline="\n")
    elif args.command == "ablation-report":
        results = json.loads(args.results.read_text("utf-8"))
        args.out.write_text(ablation.report(results), "utf-8", newline="\n")
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


def ablation_results(
    admin_dsn: str,
    live: bool,
    budget_usd: Decimal | None,
    case_ids: Sequence[str] | None,
    effort: str = ablation.EFFORT,
) -> tuple[dict[str, object], dict[str, list[dict[str, object]]]]:
    """Investigate, then score: the labels are read only after every case is investigated.
    Returns the results and each investigated case's transcript."""
    if live:
        budget = ablation.live_budget(budget_usd, os.environ)
        model = ablation.live_model(effort)
        rows = ablation.investigate(admin_dsn, CORPUS, lambda _: model, budget, case_ids)
        name = model.name
    else:
        budget = ablation.MAX_BUDGET_USD  # a scripted model costs nothing
        rows = ablation.investigate(admin_dsn, CORPUS, ablation.dry_run_model, budget, case_ids)
        name = "scripted:dry-run"
    document = ablation.results(
        rows, labels(CORPUS), live=live, budget_usd=budget, model=name, effort=effort
    )
    return document, ablation.transcripts(rows)


def change_results(dry_run: bool, live: bool, budget_usd: Decimal | None) -> dict[str, Any]:
    """Analyze, then score: the labels are read only after every case is analyzed."""
    cases = [*build_corpus(), *build_api_cases()]
    model_for: change_analysis.ModelFactory | None = None
    name, budget = None, None
    if live:
        budget = ablation.live_budget(budget_usd, os.environ)
        model = ablation.live_model()
        model_for, name = (lambda _: model), model.name
    elif dry_run:
        model_for, name = change_analysis.dry_run_model, "scripted:dry-run"
    rows = change_analysis.analyze(cases, model_for, budget or ablation.MAX_BUDGET_USD)
    every = [*labels(CORPUS), *api_labels(CORPUS)]
    document = change_analysis.results(rows, every, model=name, live=live, budget_usd=budget)
    document["transcripts"] = {r["case_id"]: r["transcript"] for r in rows if r.get("transcript")}
    return document


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
