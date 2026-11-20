"""An opt-in investigator model backed by Claude through the Anthropic SDK.

Nothing constructs this unless an operator configures it, with credentials, and with the
owner's approval of the spend (ADR-0006 owner decision: no paid service without approval).
Tests drive it through a mocked HTTP transport and make no network call.

Request shape (Claude Opus 5.5, the default):
* adaptive thinking with an explicit effort; assistant turns are passed back unchanged, so
  thinking blocks stay valid;
* ``tool_choice`` is left ``auto`` (forced tool use is rejected on this model), and each of the
  tools is ``strict``. Keywords strict schemas do not support are removed from what is sent;
  the investigator still validates every argument against the full schema. A turn the
  investigator marks ``final`` is sent with ``tool_choice`` ``none``: it cannot call a tool;
* the final answer is constrained to the ``InvestigationOutput`` JSON schema, and validated
  again by the investigator;
* server-side refusal fallbacks are on by default, and the turn records which model served it;
* prompt caching: a breakpoint on the system prompt, so that the tools and the prompt are read
  from the cache by every investigation that follows within its lifetime, and a top-level
  ``cache_control`` for the conversation, which is re-sent each turn;
* the investigator's budget notices are mid-conversation system messages placed after the tool
  results: the operator's channel, which text inside a tool result cannot imitate. They only
  ever append to the conversation, so the cached prefix stays valid. A model that does not
  take them is not supported: there is no second way of sending a notice that a tool result
  could imitate.

Cost. Every model that can be asked for has a recorded price; one without is refused, because
its spend could not be bounded. A turn's cost counts every kind of input token, every attempt
the provider reports having billed (a fallback's declined attempt included), the higher price
of a long prompt where a model has one, and the surcharge for inference kept in the United
States when the response says that is where it ran. The next turn's ceiling prices all of its
input as written to the cache, its output at ``max_tokens``, and the surcharge unless a
response has shown there is none: it stays an upper bound.

Retries. The request is sent again only when the provider said it was not processed: a rate
limit or an overload. A timeout or a dropped connection may have been processed and billed, so
it is not sent again; the paid evaluation builds its client with the SDK's own retries off for
the same reason.
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

import anthropic
from anthropic.types.beta import (
    BetaMessage,
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

Card = tuple[Decimal, Decimal, Decimal]
#: USD per million input, output and cache-read tokens, first-party API rates (the pricing
#: page, checked 2026-11-19).
PRICES: Mapping[str, Card] = {
    "claude-opus-5-5": (Decimal(4), Decimal(20), Decimal("0.20")),
    "claude-opus-5": (Decimal(5), Decimal(25), Decimal("0.50")),
    "claude-opus-4-8": (Decimal(5), Decimal(25), Decimal("0.50")),
    "claude-fable-5-1": (Decimal(10), Decimal(50), Decimal("0.25")),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10), Decimal("0.10")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
}
#: The price of a request whose prompt (every input token, cached or not) is longer than a
#: threshold, for the models that have one.
LONG_PROMPT: Mapping[str, tuple[int, Card]] = {
    "claude-haiku-5-5": (100_000, (Decimal("0.50"), Decimal("2.50"), Decimal("0.05"))),
}
#: A five-minute cache entry is written at this multiple of the input price.
CACHE_WRITE = Decimal("1.25")
#: Every token costs this multiple when inference is kept in the United States.
US_ONLY = Decimal("1.1")

RETRIES = 2
"""How often a request the provider did not process is sent again."""
RETRY_SECONDS = (5.0, 20.0)

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
    "model_context_window_exceeded": "max_tokens",
}


def strict_schema(schema: Any) -> Any:
    """A JSON Schema reduced to what strict tools and structured outputs accept.

    Every object forbids extra properties and requires all of its properties (optional values
    are nullable instead). Removed constraints are still enforced client-side; a property's
    ``description`` is kept, and is where a limit the schema cannot carry is stated.
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


def _card(model: str, requested: str | None, prompt_tokens: int) -> Card:
    """The prices a turn is billed at.

    ``model`` is what the response names. A dated snapshot of the model that was asked for
    has that model's price. Anything else unknown is priced as the dearest model recorded, so
    that a cost is overstated and never understated; the turn's ``served_by`` shows it.
    """
    name = model
    if name not in PRICES and requested is not None and model.startswith(f"{requested}-"):
        name = requested
    if name not in PRICES:
        return max(PRICES.values(), key=lambda card: card[1])
    long = LONG_PROMPT.get(name)
    if long is not None and prompt_tokens > long[0]:
        return long[1]
    return PRICES[name]


def _cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write: int = 0,
    cache_read: int = 0,
    *,
    requested: str | None = None,
    us_only: bool = False,
) -> Decimal:
    """What a turn costs; ``input_tokens`` is the uncached remainder, as the API reports it."""
    prompt = input_tokens + cache_write + cache_read
    input_price, output_price, read_price = _card(model, requested, prompt)
    total = (
        input_price * input_tokens
        + input_price * CACHE_WRITE * cache_write
        + read_price * cache_read
        + output_price * output_tokens
    )
    return total * (US_ONLY if us_only else 1) / Decimal(1_000_000)


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if model not in PRICES:
            raise ValueError(
                f"no price is recorded for {model}, so what it spends could not be bounded. "
                f"Recorded: {', '.join(sorted(PRICES))}"
            )
        self._client = client
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._fallbacks = fallbacks
        self._cache = cache
        self._sleep = sleep
        self.name = f"anthropic:{model}"

    def open(
        self,
        *,
        system: str,
        task: str,
        tools: Sequence[ToolSpec],
        output_schema: Mapping[str, Any],
    ) -> ModelSession:
        prompt: dict[str, Any] = {"type": "text", "text": system}
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": [prompt],
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
            # The tools and the prompt are the same for every investigation: one breakpoint
            # after them, and automatic caching for the conversation that follows.
            prompt["cache_control"] = {"type": "ephemeral"}
            request["cache_control"] = {"type": "ephemeral"}
        if self._fallbacks:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return _AnthropicSession(
            self._client, request, task, fallbacks=self._fallbacks, sleep=self._sleep
        )


class _AnthropicSession:
    def __init__(
        self,
        client: anthropic.Anthropic,
        request: dict[str, Any],
        task: str,
        *,
        fallbacks: bool,
        sleep: Callable[[float], None],
    ) -> None:
        self._client = client
        self._request = request
        self._messages: list[BetaMessageParam] = [{"role": "user", "content": task}]
        self._fallbacks = fallbacks
        self._sleep = sleep
        self._context_tokens: int | None = None
        """The last turn's input (cached or not) plus output tokens: an upper bound on the
        conversation so far."""
        self._us_only: bool | None = None
        """Whether the last response says inference was kept in the United States, which
        costs more. None until a response has said."""

    def ceiling_usd(
        self, tool_results: Sequence[ToolResult], notice: str | None = None
    ) -> Decimal | None:
        """An upper bound on the next turn's cost, from the fact that a token is at least a byte.

        The input is the conversation so far (the last turn's input and output, as counted by
        the API) plus the new tool results, all priced as if written to the cache, the dearest
        way to send them; the output is at most ``max_tokens``; and the surcharge for
        inference in the United States applies unless a response has shown otherwise. With
        fallbacks on, another model may serve the turn at another price, so there is no bound.
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
            if notice:
                context += len(notice.encode()) + RESULT_OVERHEAD_TOKENS
        model = self._request["model"]
        surcharge = self._us_only is not False
        if "cache_control" in self._request:
            return _cost(
                model, 0, self._request["max_tokens"], cache_write=context, us_only=surcharge
            )
        return _cost(model, context, self._request["max_tokens"], us_only=surcharge)

    def _append(self, tool_results: Sequence[ToolResult], notice: str | None) -> None:
        """Add the tool results and the investigator's notice to the conversation.

        The notice is the operator's, so it is never inside a tool result: those are the pull
        request's data. After tool results it is a mid-conversation system message, which must
        follow a user message. With no tool results (an answer is being sent back) there is no
        user message to follow, so the notice is itself the user's turn, as the task was.
        """
        if not tool_results:
            if notice:
                self._messages.append({"role": "user", "content": notice})
            return
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
        if notice:
            self._messages.append({"role": "system", "content": notice})

    def _post(self, extra: Mapping[str, Any]) -> BetaMessage:
        response: BetaMessage = self._client.beta.messages.create(
            **self._request, **extra, messages=self._messages
        )
        return response

    def _create(self, final: bool) -> BetaMessage:
        """Send the conversation. Sent again only if the provider did not process it."""
        extra: dict[str, Any] = {"tool_choice": {"type": "none"}} if final else {}
        for wait in (*RETRY_SECONDS[:RETRIES], None):
            try:
                try:
                    return self._post(extra)
                except anthropic.BadRequestError:
                    if not extra:
                        raise
                    # A turn that may not call a tool is a rare last resort, and this request
                    # shape has no other use here. If it is refused, which is not billed, the
                    # turn is sent as any other: the notice still asks for the answer.
                    extra = {}
                    return self._post(extra)
            except (anthropic.RateLimitError, anthropic.OverloadedError):
                if wait is None:
                    raise
                self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    def send(
        self,
        tool_results: Sequence[ToolResult],
        notice: str | None = None,
        *,
        final: bool = False,
    ) -> ModelTurn:
        self._append(tool_results, notice)
        response = self._create(final)
        # Append the assistant content exactly as returned, thinking blocks included.
        self._messages.append({"role": "assistant", "content": response.content})
        usage = response.usage
        written = usage.cache_creation_input_tokens or 0
        read = usage.cache_read_input_tokens or 0
        prompt = usage.input_tokens + written + read
        self._context_tokens = prompt + usage.output_tokens
        self._us_only = getattr(usage, "inference_geo", None) == "us"
        requested = str(self._request["model"])
        # With a fallback, the top-level usage is the attempt that answered; the attempts
        # billed are listed one by one, a declined one included.
        attempts = [
            entry
            for entry in (getattr(usage, "iterations", None) or [])
            if getattr(entry, "type", None) in ("message", "fallback_message")
        ]
        if attempts:
            cost = sum(
                (
                    _cost(
                        str(getattr(entry, "model", None) or response.model),
                        entry.input_tokens,
                        entry.output_tokens,
                        entry.cache_creation_input_tokens or 0,
                        entry.cache_read_input_tokens or 0,
                        requested=requested,
                        us_only=bool(self._us_only),
                    )
                    for entry in attempts
                ),
                Decimal(0),
            )
        else:
            cost = _cost(
                response.model,
                usage.input_tokens,
                usage.output_tokens,
                written,
                read,
                requested=requested,
                us_only=bool(self._us_only),
            )
        stop = _STOPS.get(str(response.stop_reason))
        text = "".join(b.text for b in response.content if isinstance(b, BetaTextBlock)) or None
        return ModelTurn(
            # A stop the loop has no use for is still a turn that was billed.
            stop=stop or "unexpected",
            tool_uses=tuple(
                ToolUse(id=block.id, name=block.name, arguments=dict(block.input))
                for block in response.content
                if isinstance(block, BetaToolUseBlock)
            ),
            text=text if stop is not None else f"stop reason {response.stop_reason!r}",
            input_tokens=prompt,
            output_tokens=usage.output_tokens,
            cache_write_tokens=written,
            cache_read_tokens=read,
            cost_usd=cost,
            served_by=f"anthropic:{response.model}",
        )
