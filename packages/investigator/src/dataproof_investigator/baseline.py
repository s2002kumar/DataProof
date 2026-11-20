"""The model-off baseline: a deterministic diagnosis from the recorded mismatches alone.

It is what an investigation is measured against (gate G3), and what the report shows when no
model is configured. Each rule recognizes one shape of difference in a check's bounded
mismatch examples and states it as a hypothesis citing that check's evidence.
"""

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from dataproof_contracts import (
    CheckOutcome,
    CheckResult,
    Confidence,
    DecimalValue,
    Hypothesis,
    Investigation,
    InvestigationStatus,
    InvestigationUsage,
    InvestigatorRole,
    Mismatch,
    TimestampValue,
)

RULES_MODEL = "dataproof:rules-v1"
CAVEAT = (
    "Rule-based classification of the check's bounded mismatch examples; it reads no source "
    "and no data beyond the recorded evidence, and the checks alone decide the verdict."
)


def _ratio(mismatch: Mismatch) -> int | None:
    expected, observed = mismatch.expected, mismatch.observed
    if not isinstance(expected, DecimalValue) or not isinstance(observed, DecimalValue):
        return None
    if expected.value == 0:
        return None
    ratio = observed.value / expected.value
    return int(ratio) if ratio == ratio.to_integral_value() and ratio > 1 else None


def _rounded(mismatch: Mismatch) -> bool:
    expected, observed = mismatch.expected, mismatch.observed
    if not isinstance(expected, DecimalValue) or not isinstance(observed, DecimalValue):
        return False
    # PostgreSQL casts round half away from zero; other code may round half-even or truncate.
    whole = {
        expected.value.quantize(Decimal(1), rounding=mode)
        for mode in (ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN)
    }
    return observed.value in whole


def _shift_hours(mismatch: Mismatch) -> int | None:
    expected, observed = mismatch.expected, mismatch.observed
    if not isinstance(expected, TimestampValue) or not isinstance(observed, TimestampValue):
        return None
    delta = observed.value - expected.value
    hours, remainder = divmod(delta, timedelta(hours=1))
    return int(hours) if remainder == timedelta(0) and hours != 0 else None


def _permuted(examples: tuple[Mismatch, ...]) -> bool:
    changed = [m for m in examples if m.expected is not None and m.observed is not None]
    if len(changed) < 2:
        return False
    by_column: dict[str | None, tuple[Counter[str], Counter[str]]] = {}
    for m in changed:
        expected, observed = by_column.setdefault(m.column, (Counter(), Counter()))
        expected[m.expected.model_dump_json()] += 1  # type: ignore[union-attr]
        observed[m.observed.model_dump_json()] += 1  # type: ignore[union-attr]
    return any(len(e) > 1 and e == o for e, o in by_column.values())


def _statements(check: CheckResult) -> list[str]:
    examples = check.mismatch_examples
    statements = []
    if any(m.observed is None for m in examples):
        statements.append(
            "Keys present on the reference are missing from the candidate: rows were lost or "
            "filtered out."
        )
    if any(m.expected is None for m in examples):
        statements.append(
            "The candidate has keys the reference lacks: rows were duplicated or introduced."
        )
    changed = [m for m in examples if m.expected is not None and m.observed is not None]
    ratios = {_ratio(m) for m in changed}
    if changed and None not in ratios:
        factors = ", ".join(f"{r}x" for r in sorted(r for r in ratios if r))
        statements.append(
            f"Every changed value is an exact multiple ({factors}) of the reference value: "
            "consistent with a join that repeats each row before it is summed or copied."
        )
    elif changed and all(_rounded(m) for m in changed):
        statements.append(
            "Changed values equal the reference values rounded to whole units: a cast or column "
            "type lost the fractional part."
        )
    elif changed and None not in {_shift_hours(m) for m in changed}:
        hours = sorted({_shift_hours(m) for m in changed}, key=lambda h: h or 0)
        statements.append(
            f"Changed instants moved by whole hours ({', '.join(str(h) for h in hours)}): a "
            "time-zone conversion was applied to values that were already instants."
        )
    elif _permuted(examples):
        statements.append(
            "The same values appear on different keys: rows were matched by an order that is "
            "not unique (ties), so values moved between them."
        )
    elif changed:
        statements.append("Values differ in ways these rules do not recognize.")
    return statements


def diagnose(run_id: UUID, checks: Mapping[str, CheckResult]) -> Investigation:
    """Explain every failed check from its recorded mismatches, deterministically."""
    started_at = datetime.now(UTC)
    hypotheses = []
    for check in sorted(checks.values(), key=lambda c: (not c.required, c.check_id)):
        if check.outcome is not CheckOutcome.FAIL:
            continue
        for statement in _statements(check):
            hypotheses.append(
                Hypothesis(
                    statement=f"{check.check_id}: {statement}",
                    cited_evidence_ids=check.evidence_ids,
                    confidence=Confidence.MEDIUM,
                )
            )
    completed = bool(hypotheses)
    return Investigation(
        investigation_id=uuid4(),
        run_id=run_id,
        role=InvestigatorRole.FAILURE_INVESTIGATOR,
        status=InvestigationStatus.COMPLETED if completed else InvestigationStatus.NOT_AVAILABLE,
        model=RULES_MODEL,
        prompt_version="rules/1",
        tool_version="none",
        hypotheses=tuple(hypotheses),
        caveat=CAVEAT if completed else None,
        failure=None if completed else "no failed check to explain",
        usage=InvestigationUsage(cost_usd=Decimal(0), latency_ms=0),
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
