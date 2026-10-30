"""Human decisions, stored separately from machine evidence (ADR-0005, PRODUCT)."""

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, model_validator

from dataproof_contracts._base import Contract, GitHubLogin, GitSha, NonEmptyText, Sha256Hex
from dataproof_contracts.enums import BINDING_DECISIONS, ApproverSeparation, HumanDecisionAction


class HumanDecision(Contract):
    """A recorded human action. It never modifies a check outcome.

    ``APPROVE_BINDING`` is bound to the head SHA and binding digest and records whether the
    approver was independent of the author (organizational) or the repository has a single
    maintainer (procedural). An organizational approval by the author is rejected.
    """

    decision_id: UUID
    run_id: UUID
    head_sha: GitSha
    action: HumanDecisionAction
    actor: GitHubLogin
    decided_at: AwareDatetime
    comment: NonEmptyText | None = None
    binding_digest: Sha256Hex | None = None
    pr_author: GitHubLogin | None = None
    approver_separation: ApproverSeparation | None = None

    @model_validator(mode="after")
    def _binding_fields(self) -> Self:
        is_binding = self.action in BINDING_DECISIONS
        if is_binding != (self.binding_digest is not None):
            raise ValueError("binding_digest is required exactly for binding decisions")
        is_approval = self.action is HumanDecisionAction.APPROVE_BINDING
        if is_approval != (self.approver_separation is not None):
            raise ValueError("approver_separation is required exactly for APPROVE_BINDING")
        if is_binding != (self.pr_author is not None):
            raise ValueError("pr_author is required exactly for binding decisions")
        if (
            self.approver_separation is ApproverSeparation.ORGANIZATIONAL
            and self.pr_author is not None
            and self.actor.casefold() == self.pr_author.casefold()
        ):
            raise ValueError("an organizational approval cannot be made by the PR author")
        return self
