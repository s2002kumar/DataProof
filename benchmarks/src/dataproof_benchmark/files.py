"""The corpus on disk: change sets a pull request could carry, and the evaluator's labels.

``benchmarks/corpus/changes/<change>/`` holds each case's revision and the ledger bindings, as a
pull request proposes them. ``benchmarks/corpus/labels.json`` is the evaluator manifest: the only
place that says what each case is. Regenerate with ``python -m dataproof_benchmark corpus`` and
verify with ``--check``; the corpus is frozen, so a difference means the generator changed.
"""

import json
from dataclasses import asdict
from pathlib import Path

import commerce_scenario
from dataproof_benchmark.corpus import CORPUS_SEED, CORPUS_VERSION, Case, build_corpus

#: The ledger bindings every case proposes (the safe ledger change set's).
BINDINGS = (
    Path(commerce_scenario.__file__).parents[2] / "changes" / "ledger-backfill-01" / "bindings"
)
PROVENANCE = (
    "Generated from parametrized templates (dataproof_benchmark.corpus) with a seed frozen "
    "before any evaluation, by the same author as the system under test: not independently "
    "authored."
)


def rendered(cases: list[Case]) -> dict[str, str]:
    """Every file of the corpus, by path relative to the corpus directory."""
    files: dict[str, str] = {}
    for case in cases:
        directory = f"changes/{case.change}"
        files[f"{directory}/{case.revision}_ledger_backfill.py"] = case.source()
        for binding in sorted(BINDINGS.glob("*.toml")):
            files[f"{directory}/bindings/{binding.name}"] = binding.read_text("utf-8")
    labels = {
        "corpus_version": CORPUS_VERSION,
        "seed": CORPUS_SEED,
        "provenance": PROVENANCE,
        "cases": [
            {
                "case_id": case.case_id,
                "change": case.change,
                "revision": case.revision,
                "holdout": case.holdout,
                **asdict(case.label),
            }
            for case in cases
        ],
    }
    files["labels.json"] = json.dumps(labels, indent=2) + "\n"
    return files


def write(root: Path) -> None:
    for relative, text in rendered(build_corpus()).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8", newline="\n")


def stale(root: Path) -> list[str]:
    expected = rendered(build_corpus())
    present = (
        {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        if root.exists()
        else set()
    )
    problems = sorted(set(expected) ^ present)
    problems += sorted(
        path
        for path, text in expected.items()
        if path in present and (root / path).read_text("utf-8") != text
    )
    return problems


def labels(root: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = json.loads((root / "labels.json").read_text("utf-8"))["cases"]
    return cases
