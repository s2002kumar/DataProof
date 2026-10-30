"""Enumerations fixed by the specification (docs/ARCHITECTURE.md, docs/PRODUCT.md)."""

from enum import StrEnum


class RunState(StrEnum):
    """Lifecycle state of a review run (ARCHITECTURE, "Run state machine").

    Binding review is deliberately *not* a state: a run never waits for a human. Checks that
    need an unapproved binding are ``NOT_RUN`` with ``NotRunReason.AWAITING_BINDING_REVIEW``,
    the run completes with verdict ``INCOMPLETE``, and an approval starts a new run.

    ``ERROR``, ``CANCELLED`` and ``TIMED_OUT`` interrupt the pipeline and lead to ``CLEANING``.
    ``COMPLETE``, ``SUPERSEDED`` and ``NOT_EXECUTED`` are terminal.
    """

    QUEUED = "QUEUED"
    FETCHING = "FETCHING"
    ANALYZING = "ANALYZING"
    PROVISIONING = "PROVISIONING"
    MIGRATING = "MIGRATING"
    VERIFYING = "VERIFYING"
    INVESTIGATING = "INVESTIGATING"
    REPRODUCING = "REPRODUCING"
    REPORTING = "REPORTING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    CLEANING = "CLEANING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    SUPERSEDED = "SUPERSEDED"
    NOT_EXECUTED = "NOT_EXECUTED"


TERMINAL_RUN_STATES = frozenset({RunState.COMPLETE, RunState.SUPERSEDED, RunState.NOT_EXECUTED})


class RunTrigger(StrEnum):
    """What created a run. Runs created by a decision on an earlier run carry its ID."""

    WEBHOOK = "webhook"
    MANUAL = "manual"
    BINDING_APPROVAL = "binding_approval"
    CORRECTION_REPLAY = "correction_replay"


DERIVED_RUN_TRIGGERS = frozenset({RunTrigger.BINDING_APPROVAL, RunTrigger.CORRECTION_REPLAY})


class CheckOutcome(StrEnum):
    """Outcome of one deterministic check (VERIFICATION, "Check outcome taxonomy")."""

    PASS = "PASS"  # noqa: S105 - a check outcome, not a credential
    FAIL = "FAIL"
    ERROR = "ERROR"
    NOT_RUN = "NOT_RUN"


class NotRunReason(StrEnum):
    """Why a check did not run. A required check that did not run makes the run INCOMPLETE."""

    AWAITING_BINDING_REVIEW = "AWAITING_BINDING_REVIEW"
    BINDING_REJECTED = "BINDING_REJECTED"
    UNSUPPORTED = "UNSUPPORTED"
    EXCLUDED = "EXCLUDED"
    TIMED_OUT = "TIMED_OUT"


class ErrorCode(StrEnum):
    """Typed technical errors named in the specification."""

    ORACLE_VIOLATED_ON_REFERENCE = "ORACLE_VIOLATED_ON_REFERENCE"
    CANDIDATE_BUILD_FAILED = "CANDIDATE_BUILD_FAILED"
    CANDIDATE_START_FAILED = "CANDIDATE_START_FAILED"
    SOURCE_DIGEST_MISMATCH = "SOURCE_DIGEST_MISMATCH"
    BINDING_UNSUPPORTED_CONSTRUCT = "BINDING_UNSUPPORTED_CONSTRUCT"
    BINDING_COVERAGE_GAP = "BINDING_COVERAGE_GAP"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    QUERY_FAILED = "QUERY_FAILED"
    RESULT_LIMIT_EXCEEDED = "RESULT_LIMIT_EXCEEDED"
    UNSUPPORTED_VALUE_TYPE = "UNSUPPORTED_VALUE_TYPE"
    #: A pipeline stage failed or lost its worker more often than the retry budget allows.
    RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"


class NotExecutedReason(StrEnum):
    """Policy reasons for refusing to execute a change at all."""

    DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL = "DEPENDENCY_CHANGE_REQUIRES_IMAGE_APPROVAL"
    FORK_PULL_REQUEST = "FORK_PULL_REQUEST"
    REPOSITORY_NOT_ALLOWLISTED = "REPOSITORY_NOT_ALLOWLISTED"


class ReportVerdict(StrEnum):
    """Derived review summary (PRODUCT, "Verdict semantics"). Never a universal safety claim."""

    BLOCKED = "BLOCKED"
    INCOMPLETE = "INCOMPLETE"
    NOT_EXECUTED = "NOT_EXECUTED"
    CHECKED = "CHECKED"
    CHECKED_WITH_WARNINGS = "CHECKED_WITH_WARNINGS"


class GitHubCheckConclusion(StrEnum):
    """GitHub Checks API conclusions that DataProof uses."""

    FAILURE = "failure"
    ACTION_REQUIRED = "action_required"
    NEUTRAL = "neutral"
    SUCCESS = "success"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class HumanDecisionAction(StrEnum):
    """Human actions recorded separately from machine evidence; none edits a check result."""

    APPROVE_BINDING = "APPROVE_BINDING"
    REJECT_BINDING = "REJECT_BINDING"
    REQUEST_CORRECTION_REPLAY = "REQUEST_CORRECTION_REPLAY"
    CANCEL_RUN = "CANCEL_RUN"


BINDING_DECISIONS = frozenset(
    {HumanDecisionAction.APPROVE_BINDING, HumanDecisionAction.REJECT_BINDING}
)


class ApproverSeparation(StrEnum):
    """Whether binding approval was separated from authorship (ADR-0005, approver rule).

    ``ORGANIZATIONAL``: the approver is not the pull request author.
    ``PROCEDURAL``: single-maintainer repository; the separation is a procedure, not a person.
    """

    ORGANIZATIONAL = "ORGANIZATIONAL"
    PROCEDURAL = "PROCEDURAL"


class BindingReviewStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Environment(StrEnum):
    REFERENCE = "REFERENCE"
    CANDIDATE = "CANDIDATE"


class FindingKind(StrEnum):
    STATIC_RISK = "STATIC_RISK"
    INTEGRITY_VIOLATION = "INTEGRITY_VIOLATION"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    BEHAVIOR_DIFFERENCE = "BEHAVIOR_DIFFERENCE"
    EXECUTION_ERROR = "EXECUTION_ERROR"


class LocalizationBasis(StrEnum):
    """Whether a source location is supported by observation or only inferred."""

    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class ComparatorKind(StrEnum):
    """How an oracle column is compared.

    ``EXACT``: typed equality. ``DECIMAL``: exact NUMERIC comparison at a declared scale with an
    optional non-negative tolerance. ``INSTANT``: timestamps compared as UTC instants.
    """

    EXACT = "EXACT"
    DECIMAL = "DECIMAL"
    INSTANT = "INSTANT"


class CleanupOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ALREADY_GONE = "ALREADY_GONE"


class RelationProvenance(StrEnum):
    """What the pull request's migrations did to a relation a binding reads (ADR-0005)."""

    CREATED = "CREATED"
    ALTERED = "ALTERED"
    UNCHANGED = "UNCHANGED"
