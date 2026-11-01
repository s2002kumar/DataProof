"""Bounded, evidence-citing AI investigation of DataProof check failures (ADR-0002)."""

from dataproof_investigator.baseline import RULES_MODEL, diagnose
from dataproof_investigator.investigator import (
    OUTPUT_SCHEMA,
    Budget,
    InvestigationOutput,
    Investigator,
)
from dataproof_investigator.model import (
    InvestigatorModel,
    ModelSession,
    ModelTurn,
    ScriptedModel,
    ToolResult,
    ToolSpec,
    ToolUse,
    answer,
    call,
)
from dataproof_investigator.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from dataproof_investigator.tools import TOOL_SPECS, TOOL_VERSION, ToolContext, run_tool

__all__ = [
    "OUTPUT_SCHEMA",
    "PROMPT_VERSION",
    "RULES_MODEL",
    "SYSTEM_PROMPT",
    "TOOL_SPECS",
    "TOOL_VERSION",
    "Budget",
    "InvestigationOutput",
    "Investigator",
    "InvestigatorModel",
    "ModelSession",
    "ModelTurn",
    "ScriptedModel",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "ToolUse",
    "answer",
    "call",
    "diagnose",
    "run_tool",
]
