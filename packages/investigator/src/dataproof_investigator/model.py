"""The interface between the investigator and a model, and a deterministic scripted model.

The investigator owns the loop, the tools and every check on what comes back; a model only
proposes tool calls and, at the end, a JSON answer. A provider adapter keeps its own
conversation state (for example to pass thinking blocks back unchanged) behind ``ModelSession``.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

StopReason = Literal["tool_use", "end", "refusal", "max_tokens"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ModelTurn:
    stop: StopReason
    tool_uses: tuple[ToolUse, ...] = ()
    text: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    served_by: str | None = None
    """``provider:model`` that produced this turn, when it differs from the one asked for
    (for example after a refusal fallback)."""


class ModelSession(Protocol):
    def send(self, tool_results: Sequence[ToolResult]) -> ModelTurn:
        """Return the model's next turn. The first call has no tool results."""
        ...


class InvestigatorModel(Protocol):
    name: str
    """``provider:model``, recorded on every investigation."""

    def open(
        self,
        *,
        system: str,
        task: str,
        tools: Sequence[ToolSpec],
        output_schema: Mapping[str, Any],
    ) -> ModelSession: ...


ScriptStep = ModelTurn | Callable[[Sequence[ToolResult]], ModelTurn]


@dataclass
class ScriptedModel:
    """Plays a fixed script of turns, ignoring the prompt: tests, demos and hostile models.

    A step may be a function of the tool results it receives, so a script can react to what
    the tools returned without any randomness.
    """

    steps: Sequence[ScriptStep]
    name: str = "scripted:test"
    received: list[Sequence[ToolResult]] = field(default_factory=list)
    opened_with: dict[str, Any] = field(default_factory=dict)

    def open(
        self,
        *,
        system: str,
        task: str,
        tools: Sequence[ToolSpec],
        output_schema: Mapping[str, Any],
    ) -> ModelSession:
        self.opened_with = {
            "system": system,
            "task": task,
            "tools": [t.name for t in tools],
            "output_schema": output_schema,
        }
        return _ScriptedSession(self)


class _ScriptedSession:
    def __init__(self, model: ScriptedModel) -> None:
        self._model = model
        self._next = 0

    def send(self, tool_results: Sequence[ToolResult]) -> ModelTurn:
        self._model.received.append(tuple(tool_results))
        if self._next >= len(self._model.steps):
            return ModelTurn(stop="end", text="")
        step = self._model.steps[self._next]
        self._next += 1
        return step(tool_results) if callable(step) else step


def call(name: str, arguments: Mapping[str, Any], use_id: str = "t1") -> ModelTurn:
    """A scripted turn that calls one tool."""
    return ModelTurn(stop="tool_use", tool_uses=(ToolUse(use_id, name, dict(arguments)),))


def answer(text: str) -> ModelTurn:
    """A scripted final turn."""
    return ModelTurn(stop="end", text=text)
