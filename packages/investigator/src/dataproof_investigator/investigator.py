"""The bounded investigation loop (ADR-0002): the model proposes, deterministic code disposes.

The loop owns every decision the model could try to take:

* only the six tools exist, and each call is validated before it runs (``tools``);
* a budget caps tool calls, turns and cost. The model is told the budget and, after every
  turn, what is left of it. A call past the limit is answered "not run"; then the model is
  asked for its answer, in a turn kept for that. An investigation that reaches a limit ends
  in an answer from the evidence it has (ADR-0009). That holds for the cost limit too, with a
  model that bounds its turns (``ModelSession.ceiling_usd``): when the money left would not
  surely cover a turn after the next one, the next one is the answer. With one that does not
  (the Anthropic adapter with fallbacks on), the cost is checked after each turn, and past the
  limit no tool is run;
* asked for its answer, a model that calls a tool instead is asked once more, in a turn that
  cannot call one. An answer the checks below reject is sent back once, with the reason. Both
  use a turn kept in reserve, and neither loosens a check. An empty answer is asked for again
  once, as it was;
* when the next turn's worst case would pass the cost limit, the results it would carry are
  withheld (they could not be afforded), and the answer is asked for in a turn that cannot
  call a tool. Only if even that turn cannot be afforded does the investigation stop unanswered;
* the final answer must parse as ``InvestigationOutput`` and every hypothesis must cite
  evidence that exists: the run's recorded evidence, or a successful tool call of this
  investigation. One unknown citation invalidates the whole answer;
* the result is an ``Investigation`` record and nothing else. The loop has no way to change a
  check, a verdict, a binding approval or the run: it never receives a handle to them.
"""

import json
import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

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
from dataproof_investigator.model import (
    InvestigatorModel,
    ModelSession,
    ModelTurn,
    ToolResult,
    ToolSpec,
    ToolUse,
)
from dataproof_investigator.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    budget_line,
    budget_notice,
    cost_notice,
    failure_task,
    insist_notice,
    repair_notice,
)
from dataproof_investigator.tools import (
    TOOL_SPECS,
    TOOL_VERSION,
    ToolContext,
    ToolDenied,
    ToolFailed,
    run_tool,
)

MAX_FAILURE_CHARS = 500
"""The longest reason an investigation records for not completing. It can quote a provider's
error or a model's answer, and neither may fill a report."""
LISTED_EVIDENCE = 40
"""The most evidence IDs a correction lists as citable."""


def clean(text: str) -> str:
    """``text`` without control characters, other than a line break and a tab.

    A model writes what it likes, a NUL included, and PostgreSQL's ``jsonb`` rejects one: a
    record that could not be stored would fail the stage that produced it.
    """
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cs"))


def _cleaned(document: Any) -> Any:
    if isinstance(document, str):
        return clean(document)
    if isinstance(document, Mapping):
        return {clean(str(key)): _cleaned(value) for key, value in document.items()}
    if isinstance(document, list | tuple):
        return [_cleaned(value) for value in document]
    return document


def _text(value: Any) -> Any:
    return clean(value) if isinstance(value, str) else value


def _optional_text(value: Any) -> Any:
    """An empty suggested action is none at all: a model asked for "a string or null" often
    writes the empty string."""
    cleaned = _text(value)
    return None if isinstance(cleaned, str) and not cleaned.strip() else cleaned


Text = Annotated[str, BeforeValidator(_text)]


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutputHypothesis(_Output):
    statement: Text = Field(
        min_length=1,
        max_length=2000,
        description="One cause, and the statement that does it (path and lines). At most "
        "2,000 characters.",
    )
    # Not cleaned: an ID with a control character in it is not the ID it resembles, and is
    # rejected as one that does not exist.
    cited_evidence_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        description="The evidence IDs that show it: at least one, at most 20. Only IDs of the "
        "run's recorded evidence or the evidence_id of your own successful tool calls.",
    )
    confidence: Confidence


class InvestigationOutput(_Output):
    """The only shape a model's final answer may take."""

    hypotheses: list[OutputHypothesis] = Field(
        min_length=1,
        max_length=10,
        description="At least one, at most 10, the most likely first.",
    )
    suggested_action: Annotated[str | None, BeforeValidator(_optional_text)] = Field(
        default=None,
        max_length=2000,
        description="The change that would fix the fault, at most 2,000 characters; null if "
        "the evidence shows none.",
    )
    caveat: Text = Field(
        min_length=1,
        max_length=1000,
        description="Required, at most 1,000 characters: what you could not establish.",
    )


OUTPUT_SCHEMA: Mapping[str, Any] = InvestigationOutput.model_json_schema()

SPARE_TURNS = 1
"""Turns kept back from the count the model is given, for one correction: asking again for an
answer, or sending back an answer that was rejected."""


@dataclass(frozen=True)
class Budget:
    max_tool_calls: int = 12
    max_turns: int = 16
    max_cost_usd: Decimal | None = Decimal("1.00")
    """The most an investigation may cost; None for no limit. A turn is not sent if the most
    it could cost would take the investigation past this, so with a model that bounds its
    turns (``ModelSession.ceiling_usd``) this is a hard limit, and the answer is asked for
    while a turn for it can still be afforded. With a model that does not bound its turns,
    the investigation stops calling tools after the turn that crosses it."""

    def __post_init__(self) -> None:
        if self.max_tool_calls < 1 or self.max_turns < 2 + SPARE_TURNS:
            raise ValueError(
                f"a budget needs at least 1 tool call and {2 + SPARE_TURNS} turns: one to "
                "call a tool, one to answer and one in reserve"
            )


@dataclass(frozen=True)
class _Rejected:
    """Why an answer was not accepted: for the record, and for the model to correct it."""

    problem: str
    advice: str


class Investigator:
    """The bounded loop. By default it is the Failure Investigator; ``change_analyst`` (in
    ``analyst``) configures the same loop with its own role, prompt, task and tools."""

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
                failure=clean(failure)[:MAX_FAILURE_CHARS] if failure else None,
                usage=InvestigationUsage(
                    input_tokens=tokens["input"],
                    output_tokens=tokens["output"],
                    cost_usd=cost,
                    latency_ms=int((time.monotonic() - clock) * 1000),
                ),
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        limit = self.budget.max_cost_usd
        max_calls, max_turns = self.budget.max_tool_calls, self.budget.max_turns
        session = self.model.open(
            system=self.system,
            task=f"{self.task(context)}\n{budget_line(max_calls, max_turns - SPARE_TURNS)}",
            tools=self.tools,
            output_schema=OUTPUT_SCHEMA,
        )
        results: list[ToolResult] = []
        notice: str | None = None
        answer_due = False  # the turn about to be sent has been asked for the answer
        final = False  # ... and may not call a tool
        corrected = False  # an answer has been sent back once already
        resent = False  # an empty answer has been asked for again
        try:
            for sent in range(1, max_turns + 1):
                ceiling = session.ceiling_usd(results, notice) if limit is not None else None
                if limit is not None and ceiling is not None and cost + ceiling > limit:
                    if not results or (final and all(r.is_error for r in results)):
                        return finish(
                            InvestigationStatus.BUDGET_EXHAUSTED,
                            failure=f"the next turn could cost up to {ceiling} USD; "
                            f"{cost} of the {limit} allowed is spent",
                        )
                    # The results cannot be afforded: they are withheld, and the answer is
                    # asked for from what the model has seen, in a turn that cannot call a tool.
                    results = [self._withheld(result, calls) for result in results]
                    notice, answer_due, final = cost_notice(), True, True
                    ceiling = session.ceiling_usd(results, notice)
                    if ceiling is not None and cost + ceiling > limit:
                        return finish(
                            InvestigationStatus.BUDGET_EXHAUSTED,
                            failure=f"even an answer could cost up to {ceiling} USD; "
                            f"{cost} of the {limit} allowed is spent",
                        )
                turn = _send(session, results, notice, final)
                tokens["input"] += turn.input_tokens
                tokens["output"] += turn.output_tokens
                cost += turn.cost_usd
                if turn.served_by:
                    served["by"] = turn.served_by
                if turn.stop == "refusal":
                    return finish(InvestigationStatus.NOT_AVAILABLE, failure="the model declined")
                if turn.stop == "max_tokens":
                    return finish(
                        InvestigationStatus.INVALID_OUTPUT,
                        failure="the answer was cut off at the token limit",
                    )
                if turn.stop == "unexpected":
                    return finish(
                        InvestigationStatus.NOT_AVAILABLE,
                        failure=f"the provider ended the turn unexpectedly: {turn.text}",
                    )
                turns_left = max_turns - sent
                if turn.stop == "end" and not (turn.text or "").strip() and not turn.tool_uses:
                    # The provider documents empty turns. Nothing of one was kept, so the same
                    # request is sent once more; a second empty answer is no answer.
                    if resent or turns_left == 0:
                        return finish(
                            InvestigationStatus.INVALID_OUTPUT,
                            failure="the model gave an empty answer twice",
                        )
                    resent = True
                    results, notice = [], None
                    continue
                if turn.stop == "end":
                    # An answer that was paid for is judged, even if it took the cost past
                    # the limit: throwing it away would save nothing.
                    checked = self._checked(turn.text, context, calls)
                    if isinstance(checked, InvestigationOutput):
                        return finish(InvestigationStatus.COMPLETED, output=checked)
                    if corrected or turns_left == 0:
                        return finish(InvestigationStatus.INVALID_OUTPUT, failure=checked.problem)
                    corrected = True
                    results, notice = [], repair_notice(checked.advice)
                    answer_due = final = True
                    continue
                if limit is not None and cost > limit:
                    return finish(
                        InvestigationStatus.BUDGET_EXHAUSTED,
                        failure=f"spent {cost} USD of the {limit} allowed",
                    )
                if not turn.tool_uses:
                    return finish(
                        InvestigationStatus.INVALID_OUTPUT,
                        failure="the model stopped to call a tool and called none",
                    )
                if answer_due:
                    if final:
                        return finish(
                            InvestigationStatus.BUDGET_EXHAUSTED,
                            failure="no answer: it was asked for its answer twice, and "
                            "called a tool both times",
                        )
                    # Every call is owed a result. None is run; the answer is asked for once
                    # more, in a turn that cannot call a tool.
                    results = [self._not_run(use) for use in turn.tool_uses]
                    notice, final = insist_notice(), True
                    continue
                # Calls past the limit are answered "not run", never dropped: the model reads
                # every result it is owed, and is then asked for its answer.
                results = [
                    self._run(use.name, use.arguments, use.id, context, calls)
                    for use in turn.tool_uses
                ]
                notice, answer_due = budget_notice(
                    max_calls - len(calls), max_calls, turns_left - SPARE_TURNS
                )
                if not answer_due and limit is not None:
                    # If, at worst, the money would not cover a turn after the next one, the
                    # next one has to be the answer.
                    ahead = session.ceiling_usd(results, notice)
                    if ahead is not None and cost + 2 * ahead > limit:
                        notice, answer_due = cost_notice(), True
        except Exception as exc:  # a provider or transport failure: the report stands without it
            return finish(InvestigationStatus.NOT_AVAILABLE, failure=f"{type(exc).__name__}: {exc}")
        return finish(
            InvestigationStatus.BUDGET_EXHAUSTED,
            failure=f"no answer after {max_turns} turns",
        )

    def _not_run(self, use: ToolUse) -> ToolResult:
        spent = "not run: no more tool calls can be made in this investigation"
        return ToolResult(use.id, json.dumps({"not_run": spent}), is_error=True)

    def _withheld(self, result: ToolResult, calls: list[ToolCall]) -> ToolResult:
        """A result that ran but cannot be afforded: the model is not sent it, so it cannot
        cite it, and the call is recorded as failed."""
        if result.is_error:
            return result
        evidence_id = json.loads(result.content).get("evidence_id")
        for index, call in enumerate(calls):
            if call.evidence_id is not None and call.evidence_id == evidence_id:
                calls[index] = call.model_copy(
                    update={
                        "outcome": ToolCallOutcome.FAILED,
                        "evidence_id": None,
                        "detail": "run, and not returned: the cost budget could not cover it",
                    }
                )
        withheld = "not returned: the cost budget of this investigation cannot cover it"
        return ToolResult(result.tool_use_id, json.dumps({"not_run": withheld}), is_error=True)

    def _run(
        self,
        name: str,
        arguments: Mapping[str, Any],
        use_id: str,
        context: ToolContext,
        calls: list[ToolCall],
    ) -> ToolResult:
        if len(calls) >= self.budget.max_tool_calls:
            # Not run and not recorded: nothing was validated or executed.
            return self._not_run(ToolUse(use_id, name, arguments))
        sequence = len(calls) + 1
        recorded_arguments = _cleaned(json.loads(json.dumps(dict(arguments), default=str)))
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
                    detail=clean(str(exc))[:500],
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
                    detail=clean(str(exc))[:500],
                )
            )
            return ToolResult(use_id, json.dumps({"failed": str(exc)}), is_error=True)
        except Exception as exc:
            # A tool that broke is a failed call, not the end of the investigation: the model
            # can go on without it. Only the error's type is kept; its text can name hosts.
            calls.append(
                ToolCall(
                    sequence=sequence,
                    tool=name,
                    arguments=recorded_arguments,
                    outcome=ToolCallOutcome.FAILED,
                    detail=f"the tool could not run ({type(exc).__name__})",
                )
            )
            failed = "the call could not be run with these arguments. Go on without it."
            return ToolResult(use_id, json.dumps({"failed": failed}), is_error=True)
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

    def _checked(
        self, text: str | None, context: ToolContext, calls: Sequence[ToolCall]
    ) -> InvestigationOutput | _Rejected:
        """The answer if it may stand, or why it may not. Nothing of a rejected answer is
        kept but identifiers: its text never reaches a report."""
        try:
            output = InvestigationOutput.model_validate_json(text or "")
        except ValidationError as exc:
            error = exc.errors()[0]
            where = ".".join(
                str(part) if isinstance(part, int) or _identifier(str(part)) else "?"
                for part in error["loc"]
            )
            problem = f"the answer is not a valid investigation: {where or 'the JSON'}: " + str(
                error["msg"]
            )
            return _Rejected(problem, problem)
        known = set(context.evidence) | {c.evidence_id for c in calls if c.evidence_id}
        unknown = sorted({e for h in output.hypotheses for e in h.cited_evidence_ids} - known)
        if not unknown:
            return output
        shown = [e for e in unknown if _identifier(e)][:5]
        problem = f"cites {len(unknown)} evidence ID(s) that do not exist" + (
            f": {', '.join(shown)}" if shown else ""
        )
        citable = sorted(known)
        listed = ", ".join(citable[:LISTED_EVIDENCE]) + (
            f", and {len(citable) - LISTED_EVIDENCE} more" if len(citable) > LISTED_EVIDENCE else ""
        )
        # The model is not shown what it wrote: the operator's message carries only the count
        # and the IDs this investigation produced, never text a pull request could have put
        # into a citation.
        advice = f"it cites {len(unknown)} evidence ID(s) that do not exist. The evidence IDs "
        return _Rejected(problem, advice + f"you can cite are: {listed}")


def _send(
    session: ModelSession, results: Sequence[ToolResult], notice: str | None, final: bool
) -> ModelTurn:
    # ``final`` is passed only when it is set, so a session written before it existed works.
    if final:
        return session.send(results, notice, final=True)
    return session.send(results, notice)


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
"""The contracts' identifier: what a recorded tool name or evidence ID may be."""


def _identifier(name: str) -> bool:
    return _IDENTIFIER.fullmatch(name) is not None
