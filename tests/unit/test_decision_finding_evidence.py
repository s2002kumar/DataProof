import pytest
from pydantic import ValidationError

from conftest import ExampleLoader
from dataproof_contracts import (
    ApproverSeparation,
    EvidenceManifest,
    Finding,
    HumanDecision,
    ReportVerdict,
    github_check_conclusion,
)

APPROVAL = "HumanDecision.approve-binding-solo"
MANIFEST = "EvidenceManifest.sealed"
FINDING = "Finding.f1-refund-multiplied"


def test_solo_maintainer_approval_is_procedural(example: ExampleLoader) -> None:
    decision = HumanDecision.model_validate(example(APPROVAL))
    assert decision.actor == decision.pr_author
    assert decision.approver_separation is ApproverSeparation.PROCEDURAL


@pytest.mark.parametrize("actor", ["example-maintainer", "Example-Maintainer"])
def test_author_cannot_record_an_organizational_approval(
    example: ExampleLoader, actor: str
) -> None:
    data = {**example(APPROVAL), "actor": actor, "approver_separation": "ORGANIZATIONAL"}
    with pytest.raises(ValidationError, match="cannot be made by the PR author"):
        HumanDecision.model_validate(data)


def test_independent_reviewer_can_record_an_organizational_approval(example: ExampleLoader) -> None:
    data = {
        **example(APPROVAL),
        "actor": "another-reviewer",
        "approver_separation": "ORGANIZATIONAL",
    }
    assert HumanDecision.model_validate(data).actor == "another-reviewer"


@pytest.mark.parametrize("field", ["binding_digest", "pr_author", "approver_separation"])
def test_approval_needs_every_binding_field(example: ExampleLoader, field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        HumanDecision.model_validate({**example(APPROVAL), field: None})


def test_rejection_needs_no_separation(example: ExampleLoader) -> None:
    data = {**example(APPROVAL), "action": "REJECT_BINDING", "approver_separation": None}
    assert HumanDecision.model_validate(data).approver_separation is None


def test_non_binding_decisions_carry_no_binding_fields(example: ExampleLoader) -> None:
    data = {
        **example(APPROVAL),
        "action": "CANCEL_RUN",
        "approver_separation": None,
        "pr_author": None,
    }
    with pytest.raises(ValidationError, match="binding_digest"):
        HumanDecision.model_validate(data)


def test_integrity_findings_must_reference_a_check(example: ExampleLoader) -> None:
    Finding.model_validate(example(FINDING))
    with pytest.raises(ValidationError, match="deterministic check"):
        Finding.model_validate({**example(FINDING), "check_id": None})
    static = {**example(FINDING), "kind": "STATIC_RISK", "check_id": None}
    assert Finding.model_validate(static).check_id is None


def test_finding_without_location_has_unknown_localization(example: ExampleLoader) -> None:
    with pytest.raises(ValidationError, match="UNKNOWN"):
        Finding.model_validate({**example(FINDING), "source_location": None})


def test_finding_needs_evidence(example: ExampleLoader) -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate({**example(FINDING), "evidence_ids": []})


def test_manifest_example_is_valid(example: ExampleLoader) -> None:
    manifest = EvidenceManifest.model_validate(example(MANIFEST))
    assert manifest.evidence_ids() == {"ev-c1-amount-query"}


def test_manifest_rejects_two_snapshots_of_one_environment(example: ExampleLoader) -> None:
    data = example(MANIFEST)
    data["snapshots"][1]["environment"] = "REFERENCE"
    with pytest.raises(ValidationError, match="one snapshot per environment"):
        EvidenceManifest.model_validate(data)


def test_manifest_rejects_restore_of_unknown_snapshot(example: ExampleLoader) -> None:
    data = example(MANIFEST)
    data["restore_verifications"][0]["snapshot_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="not in this manifest"):
        EvidenceManifest.model_validate(data)


def test_manifest_rejects_duplicate_evidence_ids(example: ExampleLoader) -> None:
    data = example(MANIFEST)
    data["records"].append(dict(data["records"][0]))
    with pytest.raises(ValidationError, match="unique"):
        EvidenceManifest.model_validate(data)


def test_manifest_retention_ends_after_sealing(example: ExampleLoader) -> None:
    data = example(MANIFEST)
    data["retain_until"] = data["sealed_at"]
    with pytest.raises(ValidationError, match="retain_until"):
        EvidenceManifest.model_validate(data)


@pytest.mark.parametrize(
    ("verdict", "conclusion"),
    [
        (ReportVerdict.BLOCKED, "failure"),
        (ReportVerdict.INCOMPLETE, "action_required"),
        (ReportVerdict.NOT_EXECUTED, "neutral"),
        (ReportVerdict.CHECKED, "success"),
        (ReportVerdict.CHECKED_WITH_WARNINGS, "success"),
    ],
)
def test_github_conclusions(verdict: ReportVerdict, conclusion: str) -> None:
    assert github_check_conclusion(verdict) == conclusion


def test_only_checked_verdicts_are_green() -> None:
    green = {v for v in ReportVerdict if github_check_conclusion(v) == "success"}
    assert green == {ReportVerdict.CHECKED, ReportVerdict.CHECKED_WITH_WARNINGS}
