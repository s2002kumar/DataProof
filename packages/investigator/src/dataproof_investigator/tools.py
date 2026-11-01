"""The investigator's tools: exactly five, all read-only (THREAT_MODEL, trust boundary 4).

* ``get_check_result(check_id)`` and ``get_evidence(evidence_id)`` read the run's recorded
  results; they can only return what deterministic code already recorded.
* ``get_schema(environment, table)`` describes an application table.
* ``read_source(path, start_line, end_line)`` reads the pinned head commit's archive, and only
  under the allowlisted application and migration roots: never contracts, bindings, docs,
  benchmarks or test labels.
* ``run_readonly_query(environment, sql, max_rows)`` runs one diagnostic query. PostgreSQL
  itself first proves it is a single plain query with no hidden reads (the same check as
  bindings), then it runs as a prepared statement in a read-only transaction of the read-only
  role, with a timeout and row and byte caps.

Arguments are validated against strict schemas before anything runs. Every result goes back to
the model as JSON data; nothing a tool returns is ever interpreted as an instruction.
"""

import io
import json
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataproof_contracts import CheckResult, Environment, typed_value
from dataproof_investigator.model import ToolSpec
from dataproof_verifier import (
    BindingRejectedError,
    QueryError,
    QueryLimits,
    check_read_query,
    run_read_only,
)

TOOL_VERSION = "tools/1"
MAX_SOURCE_LINES = 200
MAX_QUERY_ROWS = 50
MAX_RESULT_BYTES = 64 * 1024
QUERY_TIMEOUT_MS = 5000
_IDENTIFIER = r"^[a-z_][a-z0-9_]{0,62}$"


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CheckArguments(_Arguments):
    check_id: str = Field(min_length=1, max_length=128)


class _EvidenceArguments(_Arguments):
    evidence_id: str = Field(min_length=1, max_length=128)


class _SchemaArguments(_Arguments):
    environment: Literal["REFERENCE", "CANDIDATE"]
    table: str = Field(pattern=_IDENTIFIER)


class _SourceArguments(_Arguments):
    path: str = Field(min_length=1, max_length=300)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class _QueryArguments(_Arguments):
    environment: Literal["REFERENCE", "CANDIDATE"]
    sql: str = Field(min_length=1, max_length=4000)
    max_rows: int = Field(ge=1, le=MAX_QUERY_ROWS)


_ARGUMENTS: dict[str, type[_Arguments]] = {
    "get_check_result": _CheckArguments,
    "get_evidence": _EvidenceArguments,
    "get_schema": _SchemaArguments,
    "read_source": _SourceArguments,
    "run_readonly_query": _QueryArguments,
}

_DESCRIPTIONS = {
    "get_check_result": "Return one recorded check result of this run by its check ID.",
    "get_evidence": "Return one recorded evidence record of this run by its evidence ID.",
    "get_schema": "Describe the columns and constraints of an application table in the "
    "reference or candidate database.",
    "read_source": f"Read lines of an application or migration source file at the pull "
    f"request's head commit (at most {MAX_SOURCE_LINES} lines per call).",
    "run_readonly_query": "Run one read-only SELECT (or WITH ... SELECT) against the reference "
    f"or candidate database and return at most max_rows (up to {MAX_QUERY_ROWS}) rows.",
}


def _schema(model: type[_Arguments]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema["additionalProperties"] = False
    return schema


TOOL_SPECS: tuple[ToolSpec, ...] = tuple(
    ToolSpec(name=name, description=_DESCRIPTIONS[name], input_schema=_schema(model))
    for name, model in _ARGUMENTS.items()
)


class ToolDenied(Exception):  # noqa: N818 - an outcome, not a program error
    """The call is refused before anything runs."""


class ToolFailed(Exception):  # noqa: N818 - an outcome, not a program error
    """The call was allowed but did not succeed."""


@dataclass(frozen=True)
class ToolContext:
    """What the tools may read, and nothing else."""

    checks: Mapping[str, CheckResult]
    evidence: Mapping[str, Mapping[str, Any]]
    source_archive: bytes
    source_roots: tuple[str, ...]
    databases: Mapping[Environment, str]
    """Read-only role connection strings, one per environment."""


def _json(document: Any) -> str:
    return json.dumps(document, sort_keys=True, default=str)


def _source_lines(context: ToolContext, path: str, start: int, end: int) -> str:
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or ".." in normalized.parts or str(normalized) != path:
        raise ToolDenied(f"not a normalized relative path: {path!r}")
    if not any(path.startswith(root) for root in context.source_roots):
        raise ToolDenied(f"{path} is outside the readable source roots")
    if end < start or end - start + 1 > MAX_SOURCE_LINES:
        raise ToolDenied(f"read at most {MAX_SOURCE_LINES} lines, with start <= end")
    with tarfile.open(fileobj=io.BytesIO(context.source_archive), mode="r:") as archive:
        try:
            member = archive.getmember(path)
        except KeyError as exc:
            raise ToolFailed(f"{path} does not exist at the head commit") from exc
        if not member.isfile():
            raise ToolDenied(f"{path} is not a regular file")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise ToolFailed(f"{path} cannot be read")
        text = extracted.read().decode("utf-8", errors="replace")
    lines = text.splitlines()[start - 1 : end]
    return _json({"path": path, "lines": {str(start + i): line for i, line in enumerate(lines)}})


_SCHEMA_QUERY = """
SELECT c.column_name, c.data_type, c.numeric_precision, c.numeric_scale, c.is_nullable,
       c.datetime_precision
FROM information_schema.columns AS c
WHERE c.table_schema = 'public' AND c.table_name = %s
ORDER BY c.ordinal_position
"""
_CONSTRAINT_QUERY = """
SELECT con.conname, con.contype::text, pg_get_constraintdef(con.oid)
FROM pg_constraint AS con
JOIN pg_class AS rel ON rel.oid = con.conrelid
JOIN pg_namespace AS ns ON ns.oid = rel.relnamespace
WHERE ns.nspname = 'public' AND rel.relname = %s
ORDER BY con.conname
"""


def _connect(context: ToolContext, environment: str) -> psycopg.Connection[Any]:
    return psycopg.connect(context.databases[Environment(environment)], autocommit=True)


def _schema_of(context: ToolContext, environment: str, table: str) -> str:
    # Fixed, trusted catalog queries with the table name bound as a parameter.
    with _connect(context, environment) as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = 5000")  # QUERY_TIMEOUT_MS
        columns = conn.execute(_SCHEMA_QUERY, (table,), prepare=True).fetchall()
        constraints = conn.execute(_CONSTRAINT_QUERY, (table,), prepare=True).fetchall()
    if not columns:
        raise ToolFailed(f"no table {table} in the {environment.lower()} database")
    return _json(
        {
            "environment": environment,
            "table": table,
            "columns": [
                {
                    "name": name,
                    "type": data_type,
                    "precision": precision,
                    "scale": scale,
                    "nullable": nullable == "YES",
                    "datetime_precision": datetime_precision,
                }
                for name, data_type, precision, scale, nullable, datetime_precision in columns
            ],
            "constraints": [
                {"name": name, "kind": kind, "definition": definition}
                for name, kind, definition in constraints
            ],
        }
    )


def _query(context: ToolContext, environment: str, sql: str, max_rows: int) -> str:
    limits = QueryLimits(statement_timeout_ms=QUERY_TIMEOUT_MS, max_rows=max_rows)
    with _connect(context, environment) as conn:
        try:
            check_read_query(conn, sql, limits)
        except BindingRejectedError as exc:
            raise ToolDenied(str(exc)) from exc
        except QueryError as exc:
            raise ToolFailed(str(exc)) from exc
        try:
            result = run_read_only(conn, sql, limits)
        except QueryError as exc:
            raise ToolFailed(f"{exc.code.value}: {exc}") from exc
    rows = [[typed_value(value).model_dump(mode="json") for value in row] for row in result.rows]
    body = _json({"environment": environment, "columns": list(result.columns), "rows": rows})
    if len(body.encode()) > MAX_RESULT_BYTES:
        raise ToolFailed(f"the result is larger than {MAX_RESULT_BYTES} bytes; select less")
    return body


def run_tool(context: ToolContext, name: str, arguments: Mapping[str, Any]) -> str:
    """Execute one tool call and return its JSON result, or raise ToolDenied / ToolFailed."""
    model = _ARGUMENTS.get(name)
    if model is None:
        raise ToolDenied(f"there is no tool named {name!r}")
    try:
        parsed = model.model_validate(dict(arguments))
    except ValidationError as exc:
        raise ToolDenied(f"invalid arguments: {exc.errors()[0]['msg']}") from exc
    if isinstance(parsed, _CheckArguments):
        check = context.checks.get(parsed.check_id)
        if check is None:
            raise ToolFailed(f"no check {parsed.check_id!r} in this run")
        return check.model_dump_json()
    if isinstance(parsed, _EvidenceArguments):
        record = context.evidence.get(parsed.evidence_id)
        if record is None:
            raise ToolFailed(f"no evidence {parsed.evidence_id!r} in this run")
        return _json({"evidence_id": parsed.evidence_id, **record})
    if isinstance(parsed, _SchemaArguments):
        return _schema_of(context, parsed.environment, parsed.table)
    if isinstance(parsed, _SourceArguments):
        return _source_lines(context, parsed.path, parsed.start_line, parsed.end_line)
    if isinstance(parsed, _QueryArguments):
        return _query(context, parsed.environment, parsed.sql, parsed.max_rows)
    raise ToolDenied(f"there is no tool named {name!r}")  # pragma: no cover - exhaustive above
