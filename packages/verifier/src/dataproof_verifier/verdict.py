"""Derive the report verdict from check results (docs/PRODUCT.md, "Verdict semantics")."""

from collections.abc import Sequence

from dataproof_contracts import CheckOutcome, CheckResult, ReportVerdict


def derive_verdict(checks: Sequence[CheckResult]) -> ReportVerdict:
    """``BLOCKED`` beats ``INCOMPLETE`` beats warnings beats ``CHECKED``.

    A run with no required checks is ``INCOMPLETE``, never green: an empty or fully optional
    check set proves nothing. ``NOT_EXECUTED`` is decided before any check runs, so it is never
    derived here.
    """
    required = [c for c in checks if c.required]
    if any(c.outcome in (CheckOutcome.FAIL, CheckOutcome.ERROR) for c in required):
        return ReportVerdict.BLOCKED
    if not required or any(c.outcome is CheckOutcome.NOT_RUN for c in required):
        return ReportVerdict.INCOMPLETE
    if any(c.outcome is not CheckOutcome.PASS for c in checks if not c.required):
        return ReportVerdict.CHECKED_WITH_WARNINGS
    return ReportVerdict.CHECKED
