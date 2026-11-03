"""The bounded investigation loop (ADR-0002): the model proposes, deterministic code disposes.

The loop owns every decision the model could try to take:

* only the five tools exist, and each call is validated before it runs (``tools``);
* a budget caps tool calls and turns;
* the final answer must parse as ``InvestigationOutput`` and every hypothesis must cite
  evidence that exists: the run's recorded evidence, or a successful tool call of this
  investigation. One unknown citation invalidates the whole answer;
* the result is an ``Investigation`` record and nothing else. The loop has no way to change a
  check, a verdict, a binding approval or the run: it never receives a handle to them.
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataproof_contracts import (
    TOOL_EVIDENCE_PREFIX,
    Confidence,
    Hypothesis,
    Investigation,
    InvestigationStatus,
    InvestigationUsage,
    InvestigatorRole,
    ToolCall,
    ToolCallOutcome,
)
from dataproof_investigator.model import InvestigatorModel, ModelTurn, ToolResult, ToolSpec
from dataproof_investigator.prompts import PROMPT_VERSION, SYSTEM_PROMPT, failure_task
from dataproof_investigator.tools import (
    TOOL_SPECS,
    TOOL_VERSION,
    ToolContext,
    ToolDenied,
    ToolFailed,
    run_tool,
)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutputHypothesis(_Output):
    statement: str = Field(min_length=1, max_length=2000)
    cited_evidence_ids: list[str] = Field(min_length=1, max_length=20)
    confidence: Confidence


class InvestigationOutput(_Output):
    """The only shape a model's final answer may take."""

    hypotheses: list[OutputHypothesis] = Field(min_length=1, max_length=10)
    suggested_action: str | None = Field(default=None, max_length=2000)
    caveat: str = Field(min_length=1, max_length=1000)


OUTPUT_SCHEMA: Mapping[str, Any] = InvestigationOutput.model_json_schema()


@dataclass(frozen=True)
class Budget:
    max_tool_calls: int = 12
    max_turns: int = 16
    max_cost_usd: Decimal | None = None
    """The most an investigation may cost. A turn is not sent if the most it could cost would
    take the investigation past this, so with a model that bounds its turns
    (``ModelSession.ceiling_usd``) this is a hard limit. With one that does not, the
    investigation stops after the turn that crosses it."""


class _BudgetExhausted(Exception):  # noqa: N818 - an outcome, not a program error
    pass


class Investigator:
    """The bounded loop. By default it is the Failure Investigator; ``change_analyst`` (in
    ``analyst``) configures the same loop with its own role, prompt, task and one tool."""

    def __init__(
        self,
        model: InvestigatorModel,
        budget: Budget | None = None,
        *,
        role: InvestigatorRole = InvestigatorRole.FAILURE_INVESTIGATOR,
        system: str = SYSTEM_PROMPT,
        task: Callable[[ToolContext], str] = failure_task,
        tools: Sequence[ToolSpec] = TOOL_SPECS,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self.model = model
        self.budget = budget or Budget()
        self.role = role
        self.system = system
        self.task = task
        self.tools = tuple(tools)
        self.prompt_version = prompt_version
        self._allowed = frozenset(spec.name for spec in self.tools)

    def investigate(self, run_id: UUID, context: ToolContext) -> Investigation:
        """Investigate one run: by default, its failed and errored checks."""
        started_at, clock = datetime.now(UTC), time.monotonic()
        calls: list[ToolCall] = []
        tokens = {"input": 0, "output": 0}
        cost = Decimal(0)
        served = {"by": self.model.name}

        def finish(
            status: InvestigationStatus,
            *,
            output: InvestigationOutput | None = None,
            failure: str | None = None,
        ) -> Investigation:
            return Investigation(
                investigation_id=uuid4(),
                run_id=run_id,
                role=self.role,
                status=status,
                model=served["by"],
                prompt_version=self.prompt_version,
                tool_version=TOOL_VERSION,
                tool_calls=tuple(calls),
                hypotheses=tuple(
                    Hypothesis(
                        statement=h.statement,
                        cited_evidence_ids=tuple(h.cited_evidence_ids),
                        confidence=h.confidence,
                    )
                    for h in (output.hypotheses if output else [])
                ),
                suggested_action=output.suggested_action if output else None,
                caveat=output.caveat if output else None,
                failure=failure,
                usage=InvestigationUsage(
                    input_tokens=tokens["input"],
                    output_tokens=tokens["output"],
                    cost_usd=cost,
                    latency_ms=int((time.monotonic() - clock) * 1000),
                ),
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        session = self.model.open(
            system=self.system,
            task=self.task(context),
            tools=self.tools,
            output_schema=OUTPUT_SCHEMA,
        )
        results: list[ToolResult] = []
        try:
            for _ in range(self.budget.max_turns):
                limit = self.budget.max_cost_usd
                ceiling = session.ceiling_usd(results) if limit is not None else None
                if limit is not None and ceiling is not None and cost + ceiling > limit:
                    return finish(
                        InvestigationStatus.BUDGET_EXHAUSTED,
                        failure=f"the next turn could cost up to {ceiling} USD; "
                        f"{cost} of the {limit} allowed is spent",
                    )
                turn: ModelTurn = session.send(results)
                tokens["input"] += turn.input_tokens
                tokens["output"] += turn.output_tokens
                cost += turn.cost_usd
                if self.budget.max_cost_usd is not None and cost > self.budget.max_cost_usd:
                    return finish(
                        InvestigationStatus.BUDGET_EXHAUSTED,
                        failure=f"spent {cost} USD of the {self.budget.max_cost_usd} allowed",
                    )
                if turn.served_by:
                    served["by"] = turn.served_by
                if turn.stop == "refusal":
                    return finish(InvestigationStatus.NOT_AVAILABLE, failure="the model declined")
                if turn.stop == "max_tokens":
                    return finish(
                        InvestigationStatus.INVALID_OUTPUT,
                        failure="the answer was cut off at the token limit",
                    )
                if turn.stop == "end":
                    return self._conclude(turn.text, context, calls, finish)
                results = [
                    self._run(use.name, use.arguments, use.id, context, calls)
                    for use in turn.tool_uses
                ]
        except _BudgetExhausted:
            return finish(
                InvestigationStatus.BUDGET_EXHAUSTED,
                failure=f"more than {self.budget.max_tool_calls} tool calls",
            )
        except Exception as exc:  # a provider or transport failure: the report stands without it
            return finish(InvestigationStatus.NOT_AVAILABLE, failure=f"{type(exc).__name__}: {exc}")
        return finish(
            InvestigationStatus.BUDGET_EXHAUSTED,
            failure=f"no answer after {self.budget.max_turns} turns",
        )

    def _run(
        self,
        name: str,
        arguments: Mapping[str, Any],
        use_id: str,
        context: ToolContext,
        calls: list[ToolCall],
    ) -> ToolResult:
        if len(calls) >= self.budget.max_tool_calls:
            raise _BudgetExhausted
        sequence = len(calls) + 1
        recorded_arguments = json.loads(json.dumps(dict(arguments), default=str))
        try:
            if name not in self._allowed:
                raise ToolDenied(f"there is no tool named {name!r}")
            content = run_tool(context, name, arguments)
        except ToolDenied as exc:
            calls.append(
                ToolCall(
                    sequence=sequence,
                    tool=name if _identifier(name) else "unknown",
                    arguments=recorded_arguments,
                    outcome=ToolCallOutcome.DENIED,
                    detail=str(exc)[:500],
                )
            )
            return ToolResult(use_id, json.dumps({"denied": str(exc)}), is_error=True)
        except ToolFailed as exc:
            calls.append(
                ToolCall(
                    sequence=sequence,
                    tool=name,
                    arguments=recorded_arguments,
                    outcome=ToolCallOutcome.FAILED,
                    detail=str(exc)[:500],
                )
            )
            return ToolResult(use_id, json.dumps({"failed": str(exc)}), is_error=True)
        evidence_id = f"{TOOL_EVIDENCE_PREFIX}{sequence}"
        calls.append(
            ToolCall(
                sequence=sequence,
                tool=name,
                arguments=recorded_arguments,
                outcome=ToolCallOutcome.OK,
                evidence_id=evidence_id,
            )
        )
        return ToolResult(
            use_id, json.dumps({"evidence_id": evidence_id, "data": json.loads(content)})
        )

    def _conclude(
        self,
        text: str | None,
        context: ToolContext,
        calls: list[ToolCall],
        finish: Callable[..., Investigation],
    ) -> Investigation:
        try:
            output = InvestigationOutput.model_validate_json(text or "")
        except ValidationError as exc:
            return finish(
                InvestigationStatus.INVALID_OUTPUT,
                failure=f"the answer is not a valid investigation: {exc.errors()[0]['msg']}",
            )
        known = set(context.evidence) | {c.evidence_id for c in calls if c.evidence_id}
        unknown = sorted({e for h in output.hypotheses for e in h.cited_evidence_ids} - known)
        if unknown:
            return finish(
                InvestigationStatus.INVALID_OUTPUT,
                failure=f"cites evidence that does not exist: {', '.join(unknown[:5])}",
            )
        return finish(InvestigationStatus.COMPLETED, output=output)


def _identifier(name: str) -> bool:
    return bool(name) and len(name) <= 128 and all(c.isalnum() or c in "_.:-" for c in name)
