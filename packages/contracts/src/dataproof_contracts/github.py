"""Report verdict to GitHub check conclusion (docs/PRODUCT.md, "Verdict semantics")."""

from types import MappingProxyType

from dataproof_contracts.enums import GitHubCheckConclusion, ReportVerdict

CHECK_CONCLUSIONS = MappingProxyType(
    {
        ReportVerdict.BLOCKED: GitHubCheckConclusion.FAILURE,
        ReportVerdict.INCOMPLETE: GitHubCheckConclusion.ACTION_REQUIRED,
        ReportVerdict.NOT_EXECUTED: GitHubCheckConclusion.NEUTRAL,
        ReportVerdict.CHECKED: GitHubCheckConclusion.SUCCESS,
        ReportVerdict.CHECKED_WITH_WARNINGS: GitHubCheckConclusion.SUCCESS,
    }
)


def github_check_conclusion(verdict: ReportVerdict) -> GitHubCheckConclusion:
    """Map a verdict to its check conclusion. ``NOT_EXECUTED`` and ``INCOMPLETE`` are never green.

    A ``success`` must still be published with an explicit required-check coverage table.
    """
    return CHECK_CONCLUSIONS[verdict]
