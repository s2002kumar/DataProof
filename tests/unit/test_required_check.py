"""Regression for #51: only a fully checked run can pass a required DataProof check."""

import pytest

from dataproof_contracts import GitHubCheckConclusion, ReportVerdict, github_check_conclusion
from dataproof_contracts.github import GITHUB_PASSING_CONCLUSIONS

PASSES = {ReportVerdict.CHECKED, ReportVerdict.CHECKED_WITH_WARNINGS}


@pytest.mark.parametrize("verdict", list(ReportVerdict))
def test_a_required_check_passes_only_when_every_required_check_passed(
    verdict: ReportVerdict,
) -> None:
    passes = github_check_conclusion(verdict).value in GITHUB_PASSING_CONCLUSIONS
    assert passes == (verdict in PASSES), verdict


def test_not_executed_blocks_a_required_check_without_calling_the_code_broken() -> None:
    assert github_check_conclusion(ReportVerdict.NOT_EXECUTED) is (
        GitHubCheckConclusion.ACTION_REQUIRED
    )


@pytest.mark.parametrize("conclusion", list(GitHubCheckConclusion))
def test_no_conclusion_dataproof_publishes_passes_unless_it_is_success(
    conclusion: GitHubCheckConclusion,
) -> None:
    passes = conclusion.value in GITHUB_PASSING_CONCLUSIONS
    assert passes == (conclusion is GitHubCheckConclusion.SUCCESS)
