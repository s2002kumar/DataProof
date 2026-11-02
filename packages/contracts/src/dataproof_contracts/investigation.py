"""AI investigations (ARCHITECTURE, ``Investigation``; ADR-0002): advisory, cited, bounded.

An investigation explains failures with evidence; it never decides anything. Nothing here can
change a check, a verdict or an approval. A completed investigation must cite evidence for every
hypothesis and state a caveat; anything else is recorded with the reason it is not available.
"""

from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, NonNegativeInt, PositiveInt, model_validator

from dataproof_contracts._base import Contract, Identifier, NonEmptyText
from dataproof_contracts.values import ExactDecimal

#: Evidence IDs that an investigation's own successful tool calls produce.
TOOL_EVIDENCE_PREFIX = "investigation.tool."


class InvestigatorRole(StrEnum):
    CHANGE_ANALYST = "CHANGE_ANALYST"
    FAILURE_INVESTIGATOR = "FAILURE_INVESTIGATOR"


class InvestigationStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    """The model declined, failed or was not configured; the deterministic report stands."""
    INVALID_OUTPUT = "INVALID_OUTPUT"
    """The output was not valid, or cited evidence that does not exist; it is not shown."""
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ToolCallOutcome(StrEnum):
    OK = "OK"
    DENIED = "DENIED"
    """Refused before execution: an unknown tool, invalid arguments or a disallowed request."""
    FAILED = "FAILED"
    """Allowed, but it did not succeed, for example a query that timed out."""


class ToolCall(Contract):
    sequence: PositiveInt
    tool: Identifier
    arguments: dict[str, JsonValue]
    outcome: ToolCallOutcome
    evidence_id: Identifier | None = None
    """Set exactly for successful calls, so hypotheses can cite what the tool returned."""
    detail: NonEmptyText | None = None

    @model_validator(mode="after")
    def _evidence_only_for_results(self) -> Self:
        if (self.outcome is ToolCallOutcome.OK) != (self.evidence_id is not None):
            raise ValueError("evidence_id is required exactly when the call succeeded")
        if (
            self.evidence_id is not None
            and self.evidence_id != f"{TOOL_EVIDENCE_PREFIX}{self.sequence}"
        ):
            raise ValueError("a tool call's evidence ID is fixed by its sequence number")
        return self


class Hypothesis(Contract):
    statement: NonEmptyText
    cited_evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    confidence: Confidence


class InvestigationUsage(Contract):
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cost_usd: ExactDecimal
    latency_ms: NonNegativeInt


class Investigation(Contract):
    investigation_id: UUID
    run_id: UUID
    role: InvestigatorRole
    status: InvestigationStatus
    model: NonEmptyText
    """``provider:model`` that served it, for example ``dataproof:rules-v1`` for the model-off
    baseline."""
    prompt_version: NonEmptyText
    tool_version: NonEmptyText
    tool_calls: tuple[ToolCall, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    suggested_action: NonEmptyText | None = None
    caveat: NonEmptyText | None = None
    failure: NonEmptyText | None = None
    """Why the investigation is not ``COMPLETED``."""
    usage: InvestigationUsage
    started_at: AwareDatetime
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        completed = self.status is InvestigationStatus.COMPLETED
        if completed and (not self.hypotheses or self.caveat is None):
            raise ValueError("a completed investigation needs hypotheses and a caveat")
        if completed != (self.failure is None):
            raise ValueError("failure is required exactly when the investigation did not complete")
        if not completed and (self.hypotheses or self.suggested_action):
            raise ValueError("an investigation that did not complete offers no conclusions")
        sequences = [call.sequence for call in self.tool_calls]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("tool calls are numbered 1, 2, ... in order")
        produced = {c.evidence_id for c in self.tool_calls if c.evidence_id is not None}
        for hypothesis in self.hypotheses:
            for cited in hypothesis.cited_evidence_ids:
                if cited.startswith(TOOL_EVIDENCE_PREFIX) and cited not in produced:
                    raise ValueError(f"{cited} cites a tool result this investigation lacks")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self
