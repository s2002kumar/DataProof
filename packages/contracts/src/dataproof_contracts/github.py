"""Report verdict to GitHub check conclusion (docs/PRODUCT.md, "Verdict semantics")."""

from types import MappingProxyType

from dataproof_contracts.enums import GitHubCheckConclusion, ReportVerdict

#: Conclusions that GitHub branch protection counts as passing a required status check
#: (https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks).
#: DataProof publishes one of them only when every required check passed (#51).
GITHUB_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

CHECK_CONCLUSIONS = MappingProxyType(
    {
        ReportVerdict.BLOCKED: GitHubCheckConclusion.FAILURE,
        ReportVerdict.INCOMPLETE: GitHubCheckConclusion.ACTION_REQUIRED,
        # Not executed means not verified: it must block a required check, not pass it.
        ReportVerdict.NOT_EXECUTED: GitHubCheckConclusion.ACTION_REQUIRED,
        ReportVerdict.CHECKED: GitHubCheckConclusion.SUCCESS,
        ReportVerdict.CHECKED_WITH_WARNINGS: GitHubCheckConclusion.SUCCESS,
    }
)


def github_check_conclusion(verdict: ReportVerdict) -> GitHubCheckConclusion:
    """Map a verdict to its check conclusion. Only ``CHECKED`` verdicts pass a required check.

    ``NOT_EXECUTED`` and ``INCOMPLETE`` are ``action_required``: they block a required check
    without calling the code broken. A ``success`` must still be published with an explicit
    required-check coverage table.
    """
    return CHECK_CONCLUSIONS[verdict]
