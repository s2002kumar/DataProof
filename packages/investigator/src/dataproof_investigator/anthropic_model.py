"""An opt-in investigator model backed by Claude through the Anthropic SDK.

Nothing constructs this unless an operator configures it, with credentials, and with the
owner's approval of the spend (ADR-0006 owner decision: no paid service without approval).
Tests drive it through a mocked HTTP transport and make no network call.

Request shape (Claude Opus 5.5, the default):
* adaptive thinking with an explicit effort; assistant turns are passed back unchanged, so
  thinking blocks stay valid;
* ``tool_choice`` is left ``auto`` (forced tool use is rejected on this model), and each of the
  five tools is ``strict``. Keywords strict schemas do not support are removed from what is
  sent; the investigator still validates every argument against the full schema;
* the final answer is constrained to the ``InvestigationOutput`` JSON schema, and validated
  again by the investigator;
* server-side refusal fallbacks are on by default, and the turn records which model served it;
* prompt caching is automatic (a top-level ``cache_control``): each turn re-sends the
  conversation so far, and the cached prefix is billed at the cache-read price. Cost counts
  every kind of input token, and the next turn's ceiling prices all of its input at the
  cache-write price, so it stays an upper bound.
"""

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import anthropic
from anthropic.types.beta import (
    BetaMessageParam,
    BetaTextBlock,
    BetaToolResultBlockParam,
    BetaToolUseBlock,
)

from dataproof_investigator.model import (
    ModelSession,
    ModelTurn,
    StopReason,
    ToolResult,
    ToolSpec,
    ToolUse,
)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: USD per million input, output and cache-read tokens, first-party API rates (claude-api
#: skill, 2026-11). Haiku 5.5's higher rate above 100K prompt tokens is not modelled.
PRICES: Mapping[str, tuple[Decimal, Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal(4), Decimal(20), Decimal("0.20")),
    "claude-opus-5": (Decimal(5), Decimal(25), Decimal("0.50")),
    "claude-opus-4-8": (Decimal(5), Decimal(25), Decimal("0.50")),
    "claude-fable-5-1": (Decimal(10), Decimal(50), Decimal("0.25")),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10), Decimal("0.20")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
}
#: A five-minute cache entry is written at this multiple of the input price.
CACHE_WRITE = Decimal("1.25")

_UNSUPPORTED = frozenset(
    {
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "pattern",
        "title",
        "default",
    }
)
_STOPS: Mapping[str, StopReason] = {
    "tool_use": "tool_use",
    "end_turn": "end",
    "stop_sequence": "end",
    "refusal": "refusal",
    "max_tokens": "max_tokens",
}


def strict_schema(schema: Any) -> Any:
    """A JSON Schema reduced to what strict tools and structured outputs accept.

    Every object forbids extra properties and requires all of its properties (optional values
    are nullable instead). Removed constraints are still enforced client-side.
    """
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    reduced = {k: strict_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if reduced.get("type") == "object" and "properties" in reduced:
        reduced["additionalProperties"] = False
        reduced["required"] = sorted(reduced["properties"])
    return reduced


#: Tokens the API may add to a request beyond its text (tool-use instructions, message framing).
REQUEST_OVERHEAD_TOKENS = 4000
#: The same for each tool result block.
RESULT_OVERHEAD_TOKENS = 100


def _cost(
    model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0
) -> Decimal:
    """What a turn costs; ``input_tokens`` is the uncached remainder, as the API reports it."""
    input_price, output_price, read_price = PRICES.get(model, PRICES[DEFAULT_MODEL])
    total = (
        input_price * input_tokens
        + input_price * CACHE_WRITE * cache_write
        + read_price * cache_read
        + output_price * output_tokens
    )
    return total / Decimal(1_000_000)


def _bytes(value: Any) -> int:
    return len(json.dumps(value, default=str).encode())


class AnthropicModel:
    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        max_tokens: int = 16000,
        fallbacks: bool = True,
        cache: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._fallbacks = fallbacks
        self._cache = cache
        self.name = f"anthropic:{model}"

    def open(
        self,
        *,
        system: str,
        task: str,
        tools: Sequence[ToolSpec],
        output_schema: Mapping[str, Any],
    ) -> ModelSession:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": strict_schema(dict(tool.input_schema)),
                    "strict": True,
                }
                for tool in tools
            ],
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": strict_schema(dict(output_schema))},
            },
        }
        if self._cache:
            request["cache_control"] = {"type": "ephemeral"}
        if self._fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return _AnthropicSession(self._client, request, task, fallbacks=self._fallbacks)


class _AnthropicSession:
    def __init__(
        self,
        client: anthropic.Anthropic,
        request: dict[str, Any],
        task: str,
        *,
        fallbacks: bool,
    ) -> None:
        self._client = client
        self._request = request
        self._messages: list[BetaMessageParam] = [{"role": "user", "content": task}]
        self._fallbacks = fallbacks
        self._context_tokens: int | None = None
        """The last turn's input (cached or not) plus output tokens: an upper bound on the
        conversation so far."""

    def ceiling_usd(self, tool_results: Sequence[ToolResult]) -> Decimal | None:
        """An upper bound on the next turn's cost, from the fact that a token is at least a byte.

        The input is the conversation so far (the last turn's input and output, as counted by
        the API) plus the new tool results, all priced as if written to the cache, the dearest
        way to send them; the output is at most ``max_tokens``. With fallbacks on, another
        model may serve the turn at another price, so there is no bound.
        """
        if self._fallbacks:
            return None
        if self._context_tokens is None:
            prompt = _bytes(self._request["system"]) + _bytes(self._request["tools"])
            prompt += _bytes(self._request["output_config"]) + _bytes(self._messages)
            context = prompt + REQUEST_OVERHEAD_TOKENS
        else:
            context = self._context_tokens + sum(
                len(result.content.encode()) + RESULT_OVERHEAD_TOKENS for result in tool_results
            )
        model = self._request["model"]
        if "cache_control" in self._request:
            return _cost(model, 0, self._request["max_tokens"], cache_write=context)
        return _cost(model, context, self._request["max_tokens"])

    def send(self, tool_results: Sequence[ToolResult]) -> ModelTurn:
        if tool_results:
            results: list[BetaToolResultBlockParam] = [
                {
                    "type": "tool_result",
                    "tool_use_id": result.tool_use_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in tool_results
            ]
            self._messages.append({"role": "user", "content": results})
        response = self._client.beta.messages.create(**self._request, messages=self._messages)
        # Append the assistant content exactly as returned, thinking blocks included.
        self._messages.append({"role": "assistant", "content": response.content})
        stop = _STOPS.get(str(response.stop_reason))
        if stop is None:
            raise RuntimeError(f"unexpected stop reason {response.stop_reason!r}")
        usage = response.usage
        written = usage.cache_creation_input_tokens or 0
        read = usage.cache_read_input_tokens or 0
        prompt = usage.input_tokens + written + read
        self._context_tokens = prompt + usage.output_tokens
        return ModelTurn(
            stop=stop,
            tool_uses=tuple(
                ToolUse(id=block.id, name=block.name, arguments=dict(block.input))
                for block in response.content
                if isinstance(block, BetaToolUseBlock)
            ),
            text="".join(b.text for b in response.content if isinstance(b, BetaTextBlock)) or None,
            input_tokens=prompt,
            output_tokens=usage.output_tokens,
            cache_write_tokens=written,
            cache_read_tokens=read,
            cost_usd=_cost(response.model, usage.input_tokens, usage.output_tokens, written, read),
            served_by=f"anthropic:{response.model}",
        )
