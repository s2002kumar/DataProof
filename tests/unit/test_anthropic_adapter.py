"""The Anthropic adapter, offline: a mocked HTTP transport stands in for the Messages API.

No test here reaches the network or spends money. Each test checks the requests the adapter
builds and how the investigator treats the responses.
"""

import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest

from dataproof_contracts import (
    CheckOutcome,
    CheckResult,
    InvestigationStatus,
    ToolCallOutcome,
)
from dataproof_investigator import TOOL_SPECS, Budget, Investigator, ToolContext, ToolResult
from dataproof_investigator.anthropic_model import (
    FALLBACK_BETA,
    PRICES,
    AnthropicModel,
    strict_schema,
)

C1 = "commerce.c1_event_preservation"
EVIDENCE = f"{C1}:candidate:binding"
CHECK = CheckResult(
    check_id=C1,
    contract_id=C1,
    contract_version=1,
    required=True,
    outcome=CheckOutcome.FAIL,
    mismatch_count=1,
    evidence_ids=(EVIDENCE,),
    dataset_digest="d" * 64,
)
CONTEXT = ToolContext(
    checks={C1: CHECK},
    evidence={EVIDENCE: {"kind": "query", "result_sha256": "e" * 64}},
    source_archive=b"",
    source_roots=("sample-app/app/",),
    databases={},
)
FINAL = json.dumps(
    {
        "hypotheses": [
            {
                "statement": "Refund 1002 doubled.",
                "cited_evidence_ids": [EVIDENCE, "investigation.tool.1"],
                "confidence": "MEDIUM",
            }
        ],
        "suggested_action": None,
        "caveat": "Advisory.",
    }
)


def message(
    content: list[dict[str, Any]],
    stop_reason: str,
    *,
    model: str = "claude-opus-5-5",
    usage: tuple[int, int] = (1000, 200),
    cache: tuple[int, int] = (0, 0),
    **more_usage: Any,
) -> dict[str, Any]:
    """A Messages API response; ``cache`` is (tokens written to, tokens read from) the cache,
    and ``more_usage`` is any further field of its usage."""
    return {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "cache_creation_input_tokens": cache[0],
            "cache_read_input_tokens": cache[1],
            **more_usage,
        },
    }


class FakeApi:
    """Answers each request with the next scripted response and keeps every request."""

    def __init__(self, *responses: dict[str, Any] | Callable[[], httpx2.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        return response() if callable(response) else httpx2.Response(200, json=response)

    def body(self, index: int) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded

    def model(self, **options: Any) -> AnthropicModel:
        # The SDK's own retries are off, as in a paid run: a request that timed out may have
        # been billed, and is not to be sent again.
        client = anthropic.Anthropic(
            api_key="test-key-not-real",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handle)),
        )
        self.slept: list[float] = []
        return AnthropicModel(client, sleep=self.slept.append, **options)


def tool_call_then_answer(**final: Any) -> FakeApi:
    return FakeApi(
        message(
            [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_check_result",
                    "input": {"check_id": C1},
                },
            ],
            "tool_use",
        ),
        message([{"type": "text", "text": FINAL}], "end_turn", **final),
    )


def test_an_investigation_round_trip_through_the_messages_api() -> None:
    api = tool_call_then_answer()
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)

    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure
    assert investigation.model == "anthropic:claude-opus-5-5"
    assert [c.outcome for c in investigation.tool_calls] == [ToolCallOutcome.OK]
    assert investigation.usage.input_tokens == 2000
    assert investigation.usage.output_tokens == 400
    # $4 / $20 per million tokens: 2000 * 4e-6 + 400 * 20e-6
    assert investigation.usage.cost_usd == Decimal("0.016")

    first, second = api.body(0), api.body(1)
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"]["effort"] == "high"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in first, "forced tool use is rejected on this model"
    assert first["fallbacks"] == "default"
    assert FALLBACK_BETA in api.requests[0].headers["anthropic-beta"]
    assert first["cache_control"] == {"type": "ephemeral"}, "the conversation is cached"
    # And the tools and the prompt, which every investigation shares, have a breakpoint of
    # their own: the next investigation reads them from the cache.
    (prompt,) = first["system"]
    assert prompt["type"] == "text"
    assert prompt["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first["tools"]] == [t.name for t in TOOL_SPECS]
    assert all(t["strict"] is True for t in first["tools"])

    replayed_assistant = second["messages"][1]
    assert replayed_assistant["role"] == "assistant"
    assert replayed_assistant["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig-1",
    }, "thinking blocks go back unchanged"
    (result,) = second["messages"][2]["content"]
    assert (result["type"], result["tool_use_id"], result["is_error"]) == (
        "tool_result",
        "toolu_1",
        False,
    )
    assert json.loads(result["content"])["data"]["check_id"] == C1


def test_the_model_that_served_is_recorded_after_a_fallback() -> None:
    api = tool_call_then_answer(model="claude-opus-4-8")
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.model == "anthropic:claude-opus-4-8"


def test_fallbacks_can_be_turned_off() -> None:
    api = tool_call_then_answer()
    Investigator(api.model(fallbacks=False)).investigate(uuid.uuid4(), CONTEXT)
    assert "fallbacks" not in api.body(0)
    assert FALLBACK_BETA not in api.requests[0].headers.get("anthropic-beta", "")


@pytest.mark.parametrize(
    ("stop_reason", "status"),
    [
        ("refusal", InvestigationStatus.NOT_AVAILABLE),
        ("max_tokens", InvestigationStatus.INVALID_OUTPUT),
    ],
)
def test_refusals_and_truncation_are_never_conclusions(
    stop_reason: str, status: InvestigationStatus
) -> None:
    api = FakeApi(message([{"type": "text", "text": "{"}], stop_reason))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is status
    assert investigation.hypotheses == ()


def overloaded() -> httpx2.Response:
    error = {"type": "overloaded_error", "message": "busy"}
    return httpx2.Response(529, json={"type": "error", "error": error})


def test_an_api_error_leaves_the_report_alone() -> None:
    api = FakeApi(*[overloaded] * 5)
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.NOT_AVAILABLE
    assert "busy" in (investigation.failure or "") or "529" in (investigation.failure or "")
    assert len(api.requests) == 5, "sent again four times, and then given up"
    assert api.slept == [5.0, 15.0, 30.0, 60.0]


def test_a_request_the_provider_did_not_process_is_sent_again() -> None:
    def limited() -> httpx2.Response:
        error = {"type": "rate_limit_error", "message": "slow down"}
        return httpx2.Response(429, json={"type": "error", "error": error})

    api = FakeApi(limited, message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure
    assert len(api.requests) == 2
    assert api.body(0) == api.body(1), "the same request"
    assert investigation.usage.input_tokens == 1000, "the refused request cost nothing"


def test_a_request_that_may_have_been_processed_is_not_sent_again() -> None:
    """A timeout does not say whether the provider answered, and so whether it billed. The
    request is not repeated: a second copy could be billed as well, past the cost limit."""

    def timed_out() -> httpx2.Response:
        raise httpx2.ReadTimeout("no answer in time")

    api = FakeApi(timed_out, message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.NOT_AVAILABLE
    assert len(api.requests) == 1
    assert api.slept == []


def test_strict_schemas_keep_only_what_strict_mode_accepts() -> None:
    reduced = strict_schema(
        {
            "type": "object",
            "title": "Arguments",
            "properties": {
                "sql": {"type": "string", "minLength": 1, "maxLength": 4000},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 50},
                "table": {"type": "string", "pattern": "^[a-z]+$"},
                "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            },
            "required": ["sql"],
        }
    )
    assert reduced == {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "max_rows": {"type": "integer"},
            "table": {"type": "string"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["max_rows", "note", "sql", "table"],
        "additionalProperties": False,
    }


def test_no_request_is_sent_when_even_the_first_turn_could_pass_the_cost_cap() -> None:
    api = tool_call_then_answer()
    model = api.model(fallbacks=False, max_tokens=1000)
    budget = Budget(max_cost_usd=Decimal("0.0001"))
    investigation = Investigator(model, budget).investigate(uuid.uuid4(), CONTEXT)

    assert investigation.status is InvestigationStatus.BUDGET_EXHAUSTED
    assert api.requests == [], "nothing was sent, so nothing was spent"
    assert investigation.usage.cost_usd == 0


def test_a_turn_that_could_pass_the_cost_cap_is_not_sent() -> None:
    api = FakeApi(
        message(
            [{"type": "tool_use", "id": "toolu_1", "name": "get_check_result", "input": {}}],
            "tool_use",
            usage=(100_000, 1000),
        ),
        message([{"type": "text", "text": FINAL}], "end_turn"),
    )
    model = api.model(fallbacks=False, max_tokens=1000)
    # The first turn costs 100,000 * $4/M + 1,000 * $20/M = $0.42. The next one would carry at
    # least those 101,000 tokens of context, priced as cache writes (1.25 * $4/M): up to
    # $0.505 + $0.02 more, past the $0.50 cap.
    budget = Budget(max_cost_usd=Decimal("0.50"))
    investigation = Investigator(model, budget).investigate(uuid.uuid4(), CONTEXT)

    assert investigation.status is InvestigationStatus.BUDGET_EXHAUSTED
    assert len(api.requests) == 1
    assert investigation.usage.cost_usd == Decimal("0.42")
    assert "could cost up to" in (investigation.failure or "")


def test_with_fallbacks_a_turn_has_no_known_ceiling() -> None:
    session = (
        tool_call_then_answer()
        .model()
        .open(system="s", task="t", tools=TOOL_SPECS, output_schema={"type": "object"})
    )
    assert session.ceiling_usd([]) is None


def test_caching_can_be_turned_off() -> None:
    api = tool_call_then_answer()
    Investigator(api.model(cache=False)).investigate(uuid.uuid4(), CONTEXT)
    assert "cache_control" not in api.body(0)


def test_cached_tokens_are_counted_and_priced() -> None:
    api = FakeApi(
        message(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_check_result",
                    "input": {"check_id": C1},
                }
            ],
            "tool_use",
            usage=(100, 200),
            cache=(1000, 0),
        ),
        message([{"type": "text", "text": FINAL}], "end_turn", usage=(100, 200), cache=(0, 5000)),
    )
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)

    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure
    assert investigation.usage.input_tokens == 100 + 1000 + 100 + 5000, "every prompt token"
    # Uncached input at $4/M, cache writes at 1.25 * $4/M, cache reads at $0.20/M, output $20/M:
    # (200 * 4 + 1000 * 5 + 5000 * 0.2 + 400 * 20) / 1e6
    assert investigation.usage.cost_usd == Decimal("0.0148")


def test_a_cached_context_still_counts_toward_the_next_turns_ceiling() -> None:
    api = FakeApi(
        message(
            [{"type": "tool_use", "id": "toolu_1", "name": "get_check_result", "input": {}}],
            "tool_use",
            usage=(100, 1000),
            cache=(0, 100_000),
        ),
        message([{"type": "text", "text": FINAL}], "end_turn"),
    )
    model = api.model(fallbacks=False, max_tokens=1000)
    # The first turn costs 100 * $4/M + 100,000 * $0.20/M + 1,000 * $20/M = $0.0404. Its context
    # is 101,100 tokens, almost all read from the cache; the next turn could have to write it
    # all again: up to 101,100 * 1.25 * $4/M + $0.02 = $0.5255, past the $0.50 cap.
    budget = Budget(max_cost_usd=Decimal("0.50"))
    investigation = Investigator(model, budget).investigate(uuid.uuid4(), CONTEXT)

    assert investigation.status is InvestigationStatus.BUDGET_EXHAUSTED
    assert len(api.requests) == 1
    assert investigation.usage.cost_usd == Decimal("0.0404")


def two_calls_then_answer(*middle: dict[str, Any] | Callable[[], httpx2.Response]) -> FakeApi:
    def tool_use(use_id: str) -> dict[str, Any]:
        return message(
            [
                {
                    "type": "tool_use",
                    "id": use_id,
                    "name": "get_check_result",
                    "input": {"check_id": C1},
                }
            ],
            "tool_use",
        )

    return FakeApi(
        tool_use("toolu_1"), *middle, message([{"type": "text", "text": FINAL}], "end_turn")
    )


def test_the_budget_notice_is_a_system_message_after_the_tool_results() -> None:
    api = two_calls_then_answer()
    investigation = Investigator(api.model(), Budget(max_tool_calls=4, max_turns=6)).investigate(
        uuid.uuid4(), CONTEXT
    )
    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure

    first, second = api.body(0), api.body(1)
    (task,) = first["messages"]
    assert task["role"] == "user"
    assert "Budget: at most 4 tool calls and 5 turns" in task["content"]
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["user", "assistant", "user", "system"]
    results, notice = second["messages"][2], second["messages"][3]
    # The notice is the operator's: it is never inside a tool result, which is the pull
    # request's data, and the tool results carry nothing but results.
    assert [block["type"] for block in results["content"]] == ["tool_result"]
    assert notice["content"].startswith("Budget left: 3 of 4 tool calls, and 4 turns")
    assert "Budget left" not in json.dumps(results)
    # Only appended to: the earlier messages are byte for byte what was sent before.
    assert second["messages"][:1] == first["messages"]


def bad_request(text: str) -> Callable[[], httpx2.Response]:
    error = {"type": "invalid_request_error", "message": text}
    return lambda: httpx2.Response(400, json={"type": "error", "error": error})


def test_a_notice_is_never_sent_any_other_way() -> None:
    """A model that takes no system message mid-conversation is not supported. There was a
    second form, text after the tool results in the same user turn; a tool result could
    imitate it, and the prompt said operator messages never arrive that way."""
    api = two_calls_then_answer(bad_request("role 'system' is not supported on this model"))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.NOT_AVAILABLE
    assert len(api.requests) == 2, "the refused request is not sent in another form"
    assert "system-reminder" not in api.requests[1].content.decode()


def test_a_bad_request_is_not_retried() -> None:
    api = two_calls_then_answer(bad_request("max_tokens: too large"))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.NOT_AVAILABLE
    assert len(api.requests) == 2


def test_a_notice_counts_toward_the_next_turns_ceiling() -> None:
    api = tool_call_then_answer()
    session = api.model(fallbacks=False, max_tokens=1000).open(
        system="s", task="t", tools=TOOL_SPECS, output_schema={"type": "object"}
    )
    session.send([])
    without = session.ceiling_usd([])
    assert without is not None
    notice = "x" * 4000
    # 4,000 bytes and the allowance for a block, priced as a cache write at 1.25 * $4/M.
    assert session.ceiling_usd([], notice) == without + Decimal(4100) * 5 / Decimal(1_000_000)


def test_cache_reads_are_priced_at_each_models_own_rate() -> None:
    # A cache read is a twentieth of the input price on Opus 5.5 and Sonnet 5.5, and a tenth
    # on the others listed: 0.10 USD per million tokens on Sonnet 5.5, not Opus 5.5's 0.20.
    assert PRICES["claude-opus-5-5"] == (Decimal(4), Decimal(20), Decimal("0.20"))
    assert PRICES["claude-sonnet-5-5"] == (Decimal(2), Decimal(10), Decimal("0.10"))
    assert PRICES["claude-haiku-5-5"] == (Decimal("0.10"), Decimal("0.50"), Decimal("0.01"))
    sonnet = "claude-sonnet-5-5"
    served = message(
        [{"type": "text", "text": FINAL}], "end_turn", model=sonnet, cache=(0, 1_000_000)
    )
    session = (
        FakeApi(served)
        .model(model=sonnet)
        .open(system="s", task="t", tools=TOOL_SPECS, output_schema={"type": "object"})
    )
    # 1,000 uncached input tokens at $2/M, a million cache reads at $0.10/M, 200 output at $10/M.
    assert session.send([]).cost_usd == Decimal("0.104")


# What an independent review of the adapter found, before any of this met the API -----------------

FINAL_RECORDED = json.dumps(
    {
        "hypotheses": [
            {
                "statement": "Refund 1002 doubled.",
                "cited_evidence_ids": [EVIDENCE],
                "confidence": "MEDIUM",
            }
        ],
        "suggested_action": None,
        "caveat": "Advisory.",
    }
)


def open_session(api: FakeApi, **options: Any) -> Any:
    return api.model(**options).open(
        system="s", task="t", tools=TOOL_SPECS, output_schema={"type": "object"}
    )


def tool_use(use_id: str = "toolu_1", **more: Any) -> dict[str, Any]:
    block = {
        "type": "tool_use",
        "id": use_id,
        "name": "get_check_result",
        "input": {"check_id": C1},
    }
    return message([block], "tool_use", **more)


def test_a_model_with_no_recorded_price_is_refused() -> None:
    with pytest.raises(ValueError, match="no price is recorded for claude-fable-5-2"):
        FakeApi().model(model="claude-fable-5-2")


def test_a_dated_snapshot_costs_what_its_model_costs_and_a_stranger_the_dearest() -> None:
    final = [{"type": "text", "text": FINAL_RECORDED}]
    dated = open_session(FakeApi(message(final, "end_turn", model="claude-opus-5-5-20261001")))
    assert dated.send([]).cost_usd == Decimal("0.008"), "1,000 in at $4/M, 200 out at $20/M"
    # A model nobody asked for and nobody priced: never understated. The turn says who served
    # it, and a paid stage stops on that.
    stranger = open_session(FakeApi(message(final, "end_turn", model="claude-next")))
    turn = stranger.send([])
    dearest = max(PRICES.values(), key=lambda card: card[1])
    assert turn.cost_usd == (1000 * dearest[0] + 200 * dearest[1]) / Decimal(1_000_000)
    assert turn.served_by == "anthropic:claude-next"


def test_a_long_prompt_on_a_model_with_a_second_price_is_billed_at_it() -> None:
    haiku = "claude-haiku-5-5"
    final = [{"type": "text", "text": FINAL_RECORDED}]
    short = open_session(FakeApi(message(final, "end_turn", model=haiku)), model=haiku)
    assert short.send([]).cost_usd == Decimal("0.0002"), "1,000 at $0.10/M and 200 at $0.50/M"
    long = open_session(
        FakeApi(message(final, "end_turn", model=haiku, usage=(150_000, 200))), model=haiku
    )
    # Over 100,000 prompt tokens: $0.50 and $2.50 a million.
    assert long.send([]).cost_usd == Decimal("0.0755")
    # And the ceiling of the turn after it prices its context at the same higher rate:
    # 150,200 tokens written to the cache at 1.25 * $0.50/M, and 16,000 out at $2.50/M.
    session = open_session(
        FakeApi(tool_use(model=haiku, usage=(150_000, 200))), model=haiku, fallbacks=False
    )
    session.send([])
    assert session.ceiling_usd([]) == Decimal("0.133875")


def test_inference_kept_in_the_united_states_costs_a_tenth_more() -> None:
    final = [{"type": "text", "text": FINAL_RECORDED}]
    session = open_session(FakeApi(message(final, "end_turn", inference_geo="us")))
    assert session.send([]).cost_usd == Decimal("0.0088")


def test_a_ceiling_assumes_the_surcharge_until_a_response_shows_there_is_none() -> None:
    api = FakeApi(tool_use(), tool_use("toolu_2", inference_geo="us"))
    session = open_session(api, fallbacks=False, max_tokens=1000)
    first = session.ceiling_usd([])
    assert first is not None
    session.send([])  # this response names no geography: no surcharge
    plain = session.ceiling_usd([])
    # 1,200 tokens of context written to the cache at 1.25 * $4/M, and 1,000 out at $20/M.
    assert plain == Decimal("0.026")
    session.send([ToolResult("toolu_1", "{}")])  # and this one ran in the United States
    assert session.ceiling_usd([]) == Decimal("0.026") * Decimal("1.1")
    # Before any response the surcharge is assumed: the bound may not be too low.
    without = (first / Decimal("1.1")).quantize(first)
    assert first > without


def test_every_attempt_a_fallback_was_billed_for_is_counted() -> None:
    """With a fallback the response's usage is the attempt that answered. A declined attempt
    was billed too, and is listed with the others."""
    attempts = [
        {
            "type": "message",
            "model": "claude-opus-5-5",
            "input_tokens": 5000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        {
            "type": "fallback_message",
            "model": "claude-opus-4-8",
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    ]
    final = [{"type": "text", "text": FINAL_RECORDED}]
    api = FakeApi(message(final, "end_turn", model="claude-opus-4-8", iterations=attempts))
    turn = open_session(api).send([])
    # The declined attempt on Opus 5.5 (5,000 * 4 + 500 * 20) and the answer on Opus 4.8
    # (1,000 * 5 + 200 * 25), per million.
    assert turn.cost_usd == Decimal("0.03") + Decimal("0.01")
    assert turn.served_by == "anthropic:claude-opus-4-8"


@pytest.mark.parametrize(
    ("stop_reason", "status"),
    [
        ("pause_turn", InvestigationStatus.NOT_AVAILABLE),
        ("model_context_window_exceeded", InvestigationStatus.INVALID_OUTPUT),
    ],
)
def test_a_stop_the_loop_has_no_use_for_is_still_a_turn_that_was_billed(
    stop_reason: str, status: InvestigationStatus
) -> None:
    api = FakeApi(message([{"type": "text", "text": "{"}], stop_reason))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is status
    assert investigation.usage.cost_usd == Decimal("0.008"), "its cost is on the record"
    assert investigation.usage.input_tokens == 1000


def test_a_turn_that_may_not_call_a_tool_forbids_it_in_the_request() -> None:
    api = FakeApi(tool_use(), message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"))
    session = open_session(api)
    session.send([])
    session.send([ToolResult("toolu_1", "{}", is_error=True)], "Answer now.", final=True)
    assert "tool_choice" not in api.body(0)
    assert api.body(1)["tool_choice"] == {"type": "none"}
    assert [t["name"] for t in api.body(1)["tools"]] == [t.name for t in TOOL_SPECS]


def test_if_forbidding_tools_is_refused_the_turn_is_sent_as_any_other() -> None:
    final = message([{"type": "text", "text": FINAL_RECORDED}], "end_turn")
    api = FakeApi(tool_use(), bad_request("tool_choice: none is not supported here"), final)
    session = open_session(api)
    session.send([])
    turn = session.send([ToolResult("toolu_1", "{}", is_error=True)], "Answer now.", final=True)
    assert turn.stop == "end"
    assert len(api.requests) == 3
    assert "tool_choice" not in api.body(2)
    assert api.body(2)["messages"] == api.body(1)["messages"], "the same conversation"


def test_a_notice_with_no_tool_results_is_the_users_turn() -> None:
    """An answer sent back has no tool result to follow, and a system message must follow a
    user message: the notice is the user's turn, as the task was."""
    answered = message([{"type": "text", "text": "{}"}], "end_turn")
    api = FakeApi(answered, message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"))
    session = open_session(api)
    session.send([])
    session.send([], "That answer cannot be accepted.", final=True)
    messages = api.body(1)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] == "That answer cannot be accepted."


def test_an_answer_that_is_sent_back_goes_through_the_api_and_is_accepted() -> None:
    wrong = FINAL_RECORDED.replace(EVIDENCE, "commerce.c9:nothing")
    api = FakeApi(
        message([{"type": "text", "text": wrong}], "end_turn"),
        message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"),
    )
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure
    sent_back = api.body(1)["messages"][2]
    assert sent_back["role"] == "user"
    assert "cites 1 evidence ID(s) that do not exist." in sent_back["content"]
    assert "commerce.c9:nothing" not in sent_back["content"], "what it wrote is not repeated"
    assert f"The evidence IDs you can cite are: {EVIDENCE}" in sent_back["content"]
    assert api.body(1)["tool_choice"] == {"type": "none"}


# What a second review found ---------------------------------------------------------------------


def status(code: int, kind: str, **headers: str) -> Callable[[], httpx2.Response]:
    error = {"type": "error", "error": {"type": kind, "message": "try later"}}
    return lambda: httpx2.Response(code, json=error, headers=headers)


@pytest.mark.parametrize(
    ("code", "kind"),
    [(429, "rate_limit_error"), (529, "overloaded_error"), (500, "api_error"), (503, "api_error")],
)
def test_a_request_that_cannot_have_been_billed_is_sent_again(code: int, kind: str) -> None:
    """A server error is not billed, and one transient error used to end the investigation, and
    a paid stage with it."""
    answered = message([{"type": "text", "text": FINAL_RECORDED}], "end_turn")
    api = FakeApi(status(code, kind), answered)
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure
    assert len(api.requests) == 2
    assert api.slept == [5.0]


def test_a_resend_waits_as_long_as_the_provider_asks_within_a_minute() -> None:
    api = FakeApi(
        status(429, "rate_limit_error", **{"retry-after": "42"}),
        status(429, "rate_limit_error", **{"retry-after": "600"}),
        message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"),
    )
    Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert api.slept == [42.0, 60.0]


def test_a_connection_that_was_never_made_is_tried_again_and_a_dropped_one_is_not() -> None:
    def refused() -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    api = FakeApi(refused, message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure
    assert len(api.requests) == 2

    def dropped() -> httpx2.Response:
        raise httpx2.RemoteProtocolError("server disconnected without sending a response")

    api = FakeApi(dropped, message([{"type": "text", "text": FINAL_RECORDED}], "end_turn"))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.NOT_AVAILABLE
    assert len(api.requests) == 1, "it may have been processed and billed"


def test_an_empty_answer_is_not_kept_and_the_same_request_is_sent_once_more() -> None:
    """An empty assistant message cannot be sent back: the repair turn used to send one, and
    the API refuses it."""
    empty = message([{"type": "thinking", "thinking": "", "signature": "s"}], "end_turn")
    cited = FINAL_RECORDED.replace(EVIDENCE, "investigation.tool.1")
    api = FakeApi(tool_use(), empty, message([{"type": "text", "text": cited}], "end_turn"))
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.COMPLETED, investigation.failure
    assert len(api.requests) == 3
    assert api.body(2)["messages"] == api.body(1)["messages"], "the same request, nothing added"
    for request in range(3):
        for sent in api.body(request)["messages"]:
            assert sent["content"], "no empty message is ever sent"
    assert investigation.usage.input_tokens == 3000, "the empty turn is paid for and counted"

    twice = FakeApi(empty, empty)
    investigation = Investigator(twice.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.INVALID_OUTPUT
    assert investigation.failure == "the model gave an empty answer twice"


def test_an_empty_text_block_is_not_sent_back() -> None:
    use = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "get_check_result",
        "input": {"check_id": C1},
    }
    api = FakeApi(
        message([{"type": "text", "text": ""}, use], "tool_use"),
        message([{"type": "text", "text": FINAL}], "end_turn"),
    )
    Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    replayed = api.body(1)["messages"][1]["content"]
    assert [block["type"] for block in replayed] == ["tool_use"]


def test_only_a_refused_tool_choice_is_sent_again_without_it() -> None:
    final = message([{"type": "text", "text": FINAL_RECORDED}], "end_turn")
    invalid = bad_request("messages: all messages must have non-empty content")
    api = FakeApi(tool_use(), invalid, final)
    session = open_session(api)
    session.send([])
    with pytest.raises(anthropic.BadRequestError):
        session.send([ToolResult("toolu_1", "{}", is_error=True)], "Answer now.", final=True)
    assert len(api.requests) == 2, "another 400 is not taken for a refused tool_choice"


def test_an_unknown_model_is_priced_at_the_dearest_price_of_each_kind() -> None:
    from dataproof_investigator.anthropic_model import DEAREST, LONG_PROMPT

    cards = [*PRICES.values(), *(card for _, card in LONG_PROMPT.values())]
    assert tuple(max(card[i] for card in cards) for i in range(3)) == DEAREST


def test_a_turn_says_whether_it_ran_in_the_united_states() -> None:
    final = [{"type": "text", "text": FINAL_RECORDED}]
    assert open_session(FakeApi(message(final, "end_turn", inference_geo="us"))).send([]).us_only
    assert not open_session(FakeApi(message(final, "end_turn"))).send([]).us_only
