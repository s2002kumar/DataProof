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
from dataproof_investigator import TOOL_SPECS, Budget, Investigator, ToolContext
from dataproof_investigator.anthropic_model import FALLBACK_BETA, AnthropicModel, strict_schema

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
) -> dict[str, Any]:
    return {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
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
        client = anthropic.Anthropic(
            api_key="test-key-not-real",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handle)),
        )
        return AnthropicModel(client, **options)


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


def test_an_api_error_leaves_the_report_alone() -> None:
    api = FakeApi(
        lambda: httpx2.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        )
    )
    investigation = Investigator(api.model()).investigate(uuid.uuid4(), CONTEXT)
    assert investigation.status is InvestigationStatus.NOT_AVAILABLE
    assert "busy" in (investigation.failure or "") or "529" in (investigation.failure or "")


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
    # least those 101,000 tokens of context: up to $0.404 + $0.02 more, past the $0.50 cap.
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
