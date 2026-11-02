"""``python -m dataproof_benchmark``: the corpus, a run, and its report.

* ``corpus [--check]``: write (or verify) the frozen corpus from its seed.
* ``run --database <admin DSN>``: run every case at the database tier and score it; writes
  ``benchmarks/results/benchmark-results.json``. The DSN needs to create databases.
* ``report``: write ``benchmarks/results/REPORT.md`` from the results.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from dataproof_benchmark.corpus import CORPUS_SEED, CORPUS_VERSION
from dataproof_benchmark.evaluate import as_json, run
from dataproof_benchmark.files import PROVENANCE, labels, stale, write
from dataproof_benchmark.score import joined, report, summarize

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "corpus"
RESULTS = ROOT / "results" / "benchmark-results.json"
REPORT = ROOT / "results" / "REPORT.md"
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
    else:
        results = json.loads(args.results.read_text("utf-8"))
        args.out.write_text(report(results), "utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
