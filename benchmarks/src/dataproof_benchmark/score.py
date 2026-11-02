"""Score results against the evaluator's labels, and write the report.

Every rate carries its denominator. Faults that no declared contract can observe stay in the
denominators of the "all faults" figures and are shown separately, never dropped. The holdout is
reported apart from the development cases.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

BASELINES = ("B0", "B1", "B2")
FAILED = {"FAIL", "ERROR"}


def _rate(hits: int, total: int) -> dict[str, Any]:
    return {"hits": hits, "total": total, "rate": round(hits / total, 3) if total else None}


def joined(
    results: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {label["case_id"]: label for label in labels}
    if {r["case_id"] for r in results} != set(by_id):
        raise ValueError("the results do not cover exactly the labelled cases")
    cases = []
    for result in results:
        label = by_id[result["case_id"]]
        failing = sorted(c for c, outcome in result["outcomes"].items() if outcome in FAILED)
        expected = set(label["affected_events"])
        examples = set(result["c1_example_keys"])
        localized = (
            bool(label["affected_columns"])
            and result["outcomes"].get("commerce.c1_event_preservation") == "FAIL"
        )
        cases.append(
            {
                **{
                    k: label[k]
                    for k in ("case_id", "family", "kind", "variant", "holdout", "covered")
                },
                "migrated": result["migrated"],
                "migration_error": result["migration_error"],
                "outcomes": result["outcomes"],
                "error_codes": result["error_codes"],
                "b1_signals": result["b1_signals"],
                "detected_by": result["detected_by"],
                "failing": failing,
                "expected_failing": sorted(label["failing"]),
                "contracts_agree": failing == sorted(label["failing"]),
                "localization": {
                    "example_key_precision": (
                        round(len(examples & expected) / len(examples), 3) if examples else None
                    ),
                    "example_columns_match": sorted(result["c1_example_columns"])
                    == sorted(label["affected_columns"]),
                    "mismatch_count": result["c1_mismatch_count"],
                    "expected_count": len(expected),
                }
                if localized
                else None,
            }
        )
    return cases


def _summary(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    cases = list(cases)
    faults = [c for c in cases if c["kind"] == "fault"]
    covered = [c for c in faults if c["covered"]]
    cleans = [c for c in cases if c["kind"] == "clean"]
    families: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in faults:
        families[case["family"]].append(case)
    summary: dict[str, Any] = {"cases": len(cases), "faults": len(faults), "clean": len(cleans)}
    for baseline in BASELINES:
        summary[baseline] = {
            "recall_all_faults": _rate(
                sum(c["detected_by"][baseline] for c in faults), len(faults)
            ),
            "recall_covered_faults": _rate(
                sum(c["detected_by"][baseline] for c in covered), len(covered)
            ),
            "false_positives_on_clean": _rate(
                sum(c["detected_by"][baseline] for c in cleans), len(cleans)
            ),
            "by_family": {
                family: _rate(sum(c["detected_by"][baseline] for c in members), len(members))
                for family, members in sorted(families.items())
            },
        }
    localized = [c["localization"] for c in faults if c["localization"]]
    summary["B2_contracts_agree"] = _rate(sum(c["contracts_agree"] for c in cases), len(cases))
    summary["B2_localization"] = {
        "cases": len(localized),
        "example_columns_match": _rate(
            sum(entry["example_columns_match"] for entry in localized), len(localized)
        ),
        "exact_mismatch_count": _rate(
            sum(entry["mismatch_count"] == entry["expected_count"] for entry in localized),
            len(localized),
        ),
        "mean_example_key_precision": (
            round(sum(e["example_key_precision"] or 0 for e in localized) / len(localized), 3)
            if localized
            else None
        ),
    }
    return summary


def summarize(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "all": _summary(cases),
        "development": _summary(c for c in cases if not c["holdout"]),
        "holdout": _summary(c for c in cases if c["holdout"]),
    }


def _pct(rate: Mapping[str, Any]) -> str:
    return f"{rate['hits']}/{rate['total']}"


def report(results: Mapping[str, Any]) -> str:
    """The Markdown report for a results file."""
    summary = results["summary"]
    every = summary["all"]
    lines = [
        "# Benchmark report",
        "",
        "Generated by `python -m dataproof_benchmark report` from `benchmark-results.json`. "
        "Do not edit by hand.",
        "",
        "## Corpus and method",
        "",
        f"- Corpus version {results['corpus']['corpus_version']}, seed "
        f"`{results['corpus']['seed']}`: {every['faults']} faults and {every['clean']} clean "
        f"controls ({summary['holdout']['cases']} held out).",
        f"- Provenance: {results['corpus']['provenance']}",
        f"- Tier: {results['tier']}.",
        "- `B0`: the migration ran. `B1`: reconciliation over the reviewed binding view (row "
        "counts, a key-set hash, refund and capture sums per order and currency). `B2`: "
        "DataProof's deterministic contracts C1 to C3.",
        "- `B3` (B2 plus AI investigation on the same evidence) is **not run**: live model "
        "calls are a paid service that has not been approved (#7, #58).",
        "",
        "## Detection",
        "",
        "| Baseline | All faults | Faults the contracts can observe | False positives on clean "
        "controls |",
        "| --- | --- | --- | --- |",
    ]
    for baseline in BASELINES:
        entry = every[baseline]
        lines.append(
            f"| `{baseline}` | {_pct(entry['recall_all_faults'])} | "
            f"{_pct(entry['recall_covered_faults'])} | "
            f"{_pct(entry['false_positives_on_clean'])} |"
        )
    lines += [
        "",
        "### By fault family",
        "",
        "| Family | `B0` | `B1` | `B2` |",
        "| --- | --- | --- | --- |",
    ]
    for family in every["B2"]["by_family"]:
        lines.append(
            f"| {family} | "
            + " | ".join(_pct(every[b]["by_family"][family]) for b in BASELINES)
            + " |"
        )
    localization = every["B2_localization"]
    lines += [
        "",
        "## Localization and agreement",
        "",
        f"- Failing contracts agree with the labels in {_pct(every['B2_contracts_agree'])} cases.",
        f"- Of {localization['cases']} value-changing faults C1 caught, the reported columns match "
        f"in {_pct(localization['example_columns_match'])}, the full mismatch count equals the "
        f"labelled count in {_pct(localization['exact_mismatch_count'])}, and the mean precision "
        f"of the reported example keys is {localization['mean_example_key_precision']}.",
        "",
        "## Development and holdout",
        "",
        "| Split | Cases | `B1` faults | `B2` faults | `B2` false positives |",
        "| --- | --- | --- | --- | --- |",
    ]
    for split in ("development", "holdout"):
        entry = summary[split]
        lines.append(
            f"| {split} | {entry['cases']} | {_pct(entry['B1']['recall_all_faults'])} | "
            f"{_pct(entry['B2']['recall_all_faults'])} | "
            f"{_pct(entry['B2']['false_positives_on_clean'])} |"
        )
    missed = [c for c in results["cases"] if c["kind"] == "fault" and not c["detected_by"]["B2"]]
    lines += ["", "## Misses and limitations", ""]
    lines += [
        f"- `{c['case_id']}` ({c['family']}, {c['variant']}): not detected by `B2`"
        + ("; no declared contract reads what it changes." if not c["covered"] else ".")
        for c in missed
    ] or ["- Every fault was detected by `B2`."]
    lines += [
        "- API behaviour faults need live previews and run only in the pipeline tier, which this "
        "report does not cover.",
        "- The corpus was generated from templates by the author of the system under test. "
        "Close agreement with its labels shows that the labels describe the templates "
        "correctly; it does not show how DataProof does on independently authored faults.",
        "",
    ]
    return "\n".join(lines)
