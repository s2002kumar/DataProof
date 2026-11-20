"""Gates between the paid stages of the second AI evaluation (docs/AI_EVALUATION_V2.md).

A gate reads what a stage wrote and decides whether its records are a measurement and whether
the next stage may start. The first evaluation's gates lived outside the repository, untested,
and one of them passed a stage in which no model was called. These are code under test, and a
test feeds each of them that stage.

A gate never trusts a total it did not add up itself: it recomputes every turn's cost from the
turn's token counts and the price table, and compares the result with what the stage recorded.
"""

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from dataproof_benchmark import evaluation

Transcripts = Mapping[str, Sequence[Mapping[str, Any]]]


def turn_cost(turn: Mapping[str, Any]) -> Decimal | None:
    """What a turn cost, from its token counts and the price of the model that served it.

    None for a turn no priced model served (a scripted model, or a model with no recorded
    price). ``input_tokens`` counts every prompt token, cached or not, as a transcript does.
    """
    from dataproof_investigator.anthropic_model import CACHE_WRITE, PRICES

    served = str(turn.get("served_by") or "").removeprefix("anthropic:")
    if served not in PRICES:
        return None
    input_price, output_price, read_price = PRICES[served]
    written = int(turn.get("cache_write_tokens", 0))
    read = int(turn.get("cache_read_tokens", 0))
    plain = int(turn["input_tokens"]) - written - read
    total = (
        input_price * plain
        + input_price * CACHE_WRITE * written
        + read_price * read
        + output_price * int(turn["output_tokens"])
    )
    return total / Decimal(1_000_000)


def spend(kept: Transcripts) -> dict[str, Any]:
    """A stage's spend added up twice: as recorded turn by turn, and recomputed from tokens."""
    recorded, recomputed = Decimal(0), Decimal(0)
    differing: list[str] = []
    unpriced = 0
    for key, turns in sorted(kept.items()):
        for index, turn in enumerate(turns, 1):
            if "error" in turn:
                continue
            cost = Decimal(str(turn.get("cost_usd", "0")))
            recorded += cost
            again = turn_cost(turn)
            if again is None:
                unpriced += 1
            else:
                recomputed += again
                if again != cost:
                    differing.append(f"{key} turn {index}: recorded {cost}, recomputed {again}")
    return {
        "recorded_usd": recorded,
        "recomputed_usd": recomputed,
        "differing": differing,
        "unpriced_turns": unpriced,
    }


def _runs(document: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    return [
        (f"{case['case_id']}.rep{run['rep']}", run)
        for case in document["cases"]
        for run in case["runs"]
    ]


def gate(
    document: Mapping[str, Any],
    kept: Transcripts,
    *,
    kind: str,
    cap_usd: Decimal,
    live: bool,
    model: str | None = None,
    versions: Mapping[str, str] | None = None,
    need_cache_read: bool = False,
) -> dict[str, Any]:
    """Whether a stage's records are a measurement, and for a pilot whether the stage it
    precedes may start. ``reasons`` is empty exactly when ``pass`` is true.

    Every stage: it is the kind of run it claims to be, at least one investigation ran, every
    one of them sent a request, no request failed, the pinned model served every turn of a
    live stage, the recorded spend equals the spend recomputed from tokens and is within the
    cap, and a transcript exists for every run.

    A pilot, besides: every investigation answered, no turn missed a cached prefix, and (with
    ``need_cache_read``) the cache was read at all.
    """
    reasons: list[str] = []
    if document.get("dry_run") is live:
        reasons.append(f"dry_run is {document.get('dry_run')}: not the kind of run expected")
    if live and model is not None and document.get("model") != model:
        reasons.append(f"the model is {document.get('model')}, expected {model}")
    for name, expected in (versions or {}).items():
        if document.get(name) != expected:
            reasons.append(f"{name} is {document.get(name)}, expected {expected}")
    runs = _runs(document)
    if not runs:
        reasons.append("no investigation was run")
    for key, _ in runs:
        if key not in kept:
            reasons.append(f"{key}: no transcript")
        elif not kept[key]:
            reasons.append(f"{key}: no request was sent")
    listed = set(kept) - {key for key, _ in runs}
    if listed:
        reasons.append(f"transcripts without a run: {', '.join(sorted(listed)[:5])}")
    reasons += evaluation.health(kept, model if live else None)
    costs = spend(kept)
    reasons += costs["differing"][:10]
    if live and costs["unpriced_turns"]:
        reasons.append(f"{costs['unpriced_turns']} turns were served by a model with no price")
    if costs["recorded_usd"] > cap_usd:
        reasons.append(f"spent {costs['recorded_usd']} USD, more than the {cap_usd} USD cap")
    misses = evaluation.cache_misses(kept)
    statuses = Counter(str(run["status"]) for _, run in runs)
    answered = sum(bool(run.get("answered")) for _, run in runs)
    turns = [turn for transcript in kept.values() for turn in transcript if "error" not in turn]
    read = sum(int(turn.get("cache_read_tokens", 0)) for turn in turns)
    if kind == "pilot":
        for key, run in runs:
            if not run.get("answered"):
                reasons.append(f"{key}: the pilot did not answer ({run['status']})")
        reasons += misses
        if live and need_cache_read and not read:
            reasons.append("nothing was read from the prompt cache in the whole pilot")
    per_run = [Decimal(str(run["cost_usd"])) for _, run in runs]
    seconds = [int(run["latency_ms"]) / 1000 for _, run in runs]
    return {
        "pass": not reasons,
        "kind": kind,
        "live": live,
        "reasons": list(dict.fromkeys(reasons)),
        "runs": len(runs),
        "answered": answered,
        "statuses": dict(sorted(statuses.items())),
        "turns": len(turns),
        "spend": {
            key: str(value) if isinstance(value, Decimal) else value for key, value in costs.items()
        },
        "cap_usd": str(cap_usd),
        "cost_usd_median": str(statistics.median(per_run)) if per_run else None,
        "cost_usd_max": str(max(per_run)) if per_run else None,
        "seconds_median": statistics.median(seconds) if seconds else None,
        "cache_read_tokens": read,
        "cache_misses": misses,
        "not_started": dict(document.get("plumbing", {}).get("not_started", {})),
    }
