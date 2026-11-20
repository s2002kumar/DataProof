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
* ``ai-eval-audit [--check]``: recount the frozen outputs of the first live AI evaluation and
  write ``AI_EVAL_AUDIT.md`` and ``AI_EVAL_TRACES.md``; ``--check`` only verifies that the
  frozen files still match their manifest. It calls no model and no database.
* ``eval2-investigate --set dev|heldout --database <admin DSN>``, ``eval2-analyze --set ...``
  and ``eval2-report``: the second AI evaluation (docs/AI_EVALUATION_V2.md), proposed and not
  run. Without ``--live`` a scripted model runs every path at no cost, under ``.work``.
"""

import argparse
import json
import os
import platform
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from dataproof_benchmark import (
    ablation,
    audit,
    change_analysis,
    evaluation,
    pipeline_tier,
    verdicts,
)
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
    audited = commands.add_parser(
        "ai-eval-audit", help="recount the frozen outputs of the first live AI evaluation"
    )
    audited.add_argument("--check", action="store_true", help="only verify the manifest")
    for name, summary in (
        ("eval2-investigate", "evaluation v2: the Failure Investigator against the rules"),
        ("eval2-analyze", "evaluation v2: the Change Analyst against the risk rules"),
    ):
        second = commands.add_parser(name, help=summary)
        second.add_argument("--set", dest="cases", choices=("dev", "heldout"), required=True)
        second.add_argument("--live", action="store_true", help="call the paid model")
        second.add_argument("--budget-usd", type=Decimal, help="the budget approved for it")
        second.add_argument("--reps", type=int, default=1, help="investigations a case")
        second.add_argument("--model", default=ablation.MODEL)
        second.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
        second.add_argument("--workdir", type=Path, default=ROOT / ".work")
        second.add_argument("--out", type=Path)
        second.add_argument("--only", nargs="*", help="case IDs (default: every case)")
        second.add_argument("--skip", nargs="*", default=[], help="case IDs to leave out")
        second.add_argument(
            "--journal",
            type=Path,
            help="keep each turn here as it happens, and send nothing this folder already "
            "holds (a live stage always keeps one, beside its results)",
        )
        second.add_argument(
            "--stop-after-unanswered",
            type=int,
            help="the stop rule: start no investigation once this many gave no answer",
        )
        if name == "eval2-investigate":
            second.add_argument("--database", required=True, help="an admin DSN")
    second_report = commands.add_parser("eval2-report", help="write a v2 stage's report")
    second_report.add_argument("--results", type=Path, required=True)
    second_report.add_argument("--out", type=Path, required=True)
    sheet = commands.add_parser(
        "eval2-sheet", help="the held-out answers to mark, blind to which arm wrote each"
    )
    sheet.add_argument(
        "--arm",
        nargs=2,
        action="append",
        metavar=("NAME", "RESULTS"),
        required=True,
        help="an arm's name and its results file; the first arm's file gives the rules too",
    )
    sheet.add_argument("--out", type=Path, required=True, help="a folder for the sheet")
    sheet.add_argument(
        "--key", type=Path, required=True, help="where the key goes: not in the sheet's folder"
    )
    sheet.add_argument("--workdir", type=Path, default=ROOT / ".work")
    verdict = commands.add_parser("eval2-verdict", help="the acceptance criteria, from results")
    verdict.add_argument("--results", type=Path, help="the held-out investigation results")
    verdict.add_argument("--marks", type=Path, help="the owner's marks.csv")
    verdict.add_argument("--key", type=Path, help="the sheet's key")
    verdict.add_argument("--analysis", type=Path, help="the held-out Change Analyst results")
    verdict.add_argument("--analysis-dev", type=Path, help="the development analyst results")
    verdict.add_argument("--out", type=Path, required=True)
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
    elif args.command == "ai-eval-audit":
        differing = audit.changed()
        if differing:
            listed = "\n  ".join(differing)
            print(f"frozen files differ from the manifest:\n  {listed}", file=sys.stderr)
            return 1
        if not args.check:
            audit.write()
    elif args.command in ("eval2-investigate", "eval2-analyze"):
        return second_evaluation(args)
    elif args.command == "eval2-report":
        results = json.loads(args.results.read_text("utf-8"))
        written = (
            evaluation.analysis_report(results)
            if "Change Analyst" in results["evaluation"]
            else evaluation.report(results)
        )
        args.out.write_text(written, "utf-8", newline="\n")
    elif args.command == "eval2-sheet":
        return marking_sheet(args)
    elif args.command == "eval2-verdict":
        return acceptance(args)
    else:
        results = json.loads(args.results.read_text("utf-8"))
        args.out.write_text(report(results), "utf-8", newline="\n")
    return 0


def second_evaluation(args: argparse.Namespace) -> int:
    """Run one stage of the second evaluation; labels are read only when grading."""
    cases = evaluation.eval_set(args.cases, args.workdir)
    budget = ablation.live_budget(args.budget_usd, os.environ) if args.live else Decimal(10)
    investigating = args.command == "eval2-investigate"
    stage = "investigator" if investigating else "analyst"
    tokens = ablation.MAX_TOKENS if investigating else evaluation.ANALYST_MAX_TOKENS
    # The paid model is built only for a live run: a dry run needs no key and sends nothing.
    live = evaluation.live_model(args.model, args.effort, tokens) if args.live else None
    served = live.name if live is not None else "scripted:dry-run"
    name = f"eval2-{args.cases}-{stage}" + ("" if args.live else "-dry-run")
    folder = (ROOT / "results" / "ai-eval-v2") if args.live else args.workdir
    out = args.out or folder / f"{name}-results.json"
    stem = out.stem.removesuffix("-results")
    transcripts = out.with_name(f"{stem}-transcripts")
    if args.live and (out.exists() or transcripts.exists()):
        # A paid stage's results are written once. Taking a stage up again is the journal's
        # job, and it writes its results only when none exist.
        raise SystemExit(f"{out} or its transcripts already exist: a paid stage is not rerun")
    kept_in = args.journal or (out.with_name(f"{stem}-journal") if args.live else None)
    journal = evaluation.Journal(kept_in) if kept_in is not None else None
    started = evaluation.provenance(cases)  # what ran, as it was when the stage started
    try:
        return _stage(
            args, cases, budget, investigating, live, served, out, transcripts, journal, started
        )
    except evaluation.ProviderStopped as stopped:
        print(
            f"STOPPED: {stopped}\nNo results are written. The investigation it stopped is open "
            "in the journal: taking the stage up again (resume) starts it again, charged what "
            "it spent; or close the stage as it stands (abandon).",
            file=sys.stderr,
        )
        return EXIT_PROVIDER


def _stage(
    args: argparse.Namespace,
    cases: Any,
    budget: Decimal,
    investigating: bool,
    live: Any,
    served: str,
    out: Path,
    transcripts: Path,
    journal: Any,
    started: Mapping[str, Any],
) -> int:
    pinned = live.name if live is not None else None
    if investigating:
        rows = evaluation.investigate(
            args.database,
            cases,
            (lambda: live) if live is not None else evaluation.dry_run_model,
            budget,
            reps=args.reps,
            case_ids=args.only,
            skip=args.skip,
            stop_after_unanswered=args.stop_after_unanswered,
            journal=journal,
            served_by=pinned,
        )
        document = evaluation.results(
            rows,
            cases,
            live=args.live,
            model=served,
            effort=args.effort,
            reps=args.reps,
            budget_usd=budget,
        )
        kept = evaluation.transcripts(rows)
    else:
        rows = evaluation.analyze(
            cases,
            (lambda _: live) if live is not None else evaluation.dry_run_analyst,
            budget,
            reps=args.reps,
            case_ids=args.only,
            skip=args.skip,
            journal=journal,
            served_by=pinned,
            stop_after_unanswered=args.stop_after_unanswered,
        )
        document = evaluation.analysis_results(
            rows,
            cases,
            live=args.live,
            model=served,
            reps=args.reps,
            budget_usd=budget,
            effort=args.effort,
        )
        kept = evaluation.analysis_transcripts(rows)
    document["provenance"] = dict(started) | {"finished_at": document["provenance"]["written_at"]}
    problems = evaluation.health(kept, pinned)
    document["plumbing"] = {
        "problems": problems,
        "cache_misses": evaluation.cache_misses(kept),
        "not_started": {
            "halted": evaluation.not_started(document, evaluation.HALTED),
            "stop_rule": evaluation.not_started(document, evaluation.STOPPED),
            "budget": evaluation.not_started(document, "the budget"),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    # "x": never over an existing file. A dry run may be repeated, so it replaces its own.
    with out.open("x" if args.live else "w", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(document, indent=2) + "\n")
    transcripts.mkdir(exist_ok=not args.live)
    for key, turns in kept.items():
        text = json.dumps(turns, indent=2, default=str) + "\n"
        (transcripts / f"{key}.json").write_text(text, "utf-8", newline="\n")
    print(json.dumps(document["summary"], indent=2))
    print(f"results: {out}; transcripts: {transcripts}")
    return stage_exit(document, problems)


EXIT_NOT_A_MEASUREMENT = 3
EXIT_STOP_RULE = 4
EXIT_INCOMPLETE = 5
EXIT_PROVIDER = 6


def stage_exit(document: Mapping[str, Any], problems: Sequence[str]) -> int:
    """What a stage's exit code says. 0 only when every investigation it was to run ran, sent
    its requests and was served by the pinned model. The first evaluation's stages exited 0
    with no model output at all."""
    summary = document["summary"]
    expected = summary.get("investigations_expected", summary.get("analyses_expected", 0))
    ran = summary.get("investigations_run", summary.get("analyses_run", 0))
    if problems:
        listed = "\n  ".join(problems)
        print(f"this stage is not a measurement as it stands:\n  {listed}", file=sys.stderr)
        return EXIT_NOT_A_MEASUREMENT
    if expected == 0 or ran == 0:
        print("no investigation ran: this stage measured nothing", file=sys.stderr)
        return EXIT_NOT_A_MEASUREMENT
    if evaluation.stopped(document):
        print(f"stopped early: {evaluation.STOPPED}", file=sys.stderr)
        return EXIT_STOP_RULE
    if ran < expected:
        print(
            f"incomplete: {ran} of {expected} investigations ran; the rest were not started "
            "because the budget left could not cover one",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE
    return 0


def marking_sheet(args: argparse.Namespace) -> int:
    """The held-out answers for the owner to mark, shuffled, without the arm that wrote each.
    The key is written apart, to be opened only when the marks are in."""
    arms = {name: json.loads(Path(path).read_text("utf-8")) for name, path in args.arm}
    sets = {document["set"] for document in arms.values()}
    if len(sets) != 1:
        raise SystemExit(f"the arms are of different sets: {', '.join(sorted(sets))}")
    cases = evaluation.eval_set(sets.pop(), args.workdir)
    items, key = verdicts.sheet(arms, evaluation.references(cases))
    if args.key.resolve().parent == args.out.resolve():
        raise SystemExit("the key is not to be kept beside the sheet")
    args.out.mkdir(parents=True, exist_ok=True)
    args.key.parent.mkdir(parents=True, exist_ok=True)
    (args.out / "sheet.md").write_text(verdicts.sheet_markdown(items), "utf-8", newline="\n")
    (args.out / "marks.csv").write_text(verdicts.marks_csv(items), "utf-8", newline="\n")
    args.key.write_text(json.dumps(key, indent=2) + "\n", "utf-8", newline="\n")
    print(f"{len(items)} answers to mark: {args.out / 'sheet.md'}; marks in marks.csv")
    return 0


def acceptance(args: argparse.Namespace) -> int:
    """The acceptance criteria of whatever results are given, as a report and a JSON record."""
    found = []
    if args.results:
        document = json.loads(args.results.read_text("utf-8"))
        marks = verdicts.read_marks(args.marks.read_text("utf-8")) if args.marks else None
        key = json.loads(args.key.read_text("utf-8")) if args.key else None
        found.append(verdicts.verdict(document, marks, key))
    if args.analysis:
        heldout = json.loads(args.analysis.read_text("utf-8"))
        dev = json.loads(args.analysis_dev.read_text("utf-8")) if args.analysis_dev else None
        found.append(verdicts.analysis_verdict(heldout, dev))
    if not found:
        raise SystemExit("give --results, --analysis, or both")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(verdicts.verdict_markdown(entry) for entry in found)
    args.out.write_text(text, "utf-8", newline="\n")
    args.out.with_suffix(".json").write_text(
        json.dumps(found, indent=2, default=str) + "\n", "utf-8", newline="\n"
    )
    for entry in found:
        print(f"{entry['role']}: {entry['outcome']}")
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
