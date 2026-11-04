"""Markdown rendering of a run's report, shared by the GitHub App's check runs and the Action."""

from dataproof_report.markdown import (
    MAX_CELL_CHARS,
    MAX_DIAGNOSIS_CHARS,
    MAX_DIAGNOSIS_LINES,
    MAX_EXAMPLE_ROWS,
    RULES_MODEL_PREFIX,
    escape_cell,
    render_summary,
    render_title,
)

__all__ = [
    "MAX_CELL_CHARS",
    "MAX_DIAGNOSIS_CHARS",
    "MAX_DIAGNOSIS_LINES",
    "MAX_EXAMPLE_ROWS",
    "RULES_MODEL_PREFIX",
    "escape_cell",
    "render_summary",
    "render_title",
]
