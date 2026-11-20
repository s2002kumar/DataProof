"""The investigator's tools: exactly six, all read-only (THREAT_MODEL, trust boundary 4).

* ``get_briefing()`` returns, in one call, what the run already recorded about its failed
  checks, the files the pull request changed, the readable source files and the application
  tables. It reads nothing the other tools could not: it exists so that an investigation does
  not spend its budget fetching, one call at a time, what deterministic code already holds.
  It does not carry the rule-based diagnosis: a model that explains a failure is compared with
  that diagnosis, so it must not be handed it.
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

A refusal or a failure says what to do instead (ADR-0009): the first live evaluation spent 28%
of the calls it read on refusals that named what was not allowed and never what was.

Every result has a size limit, whatever the tool: a pull request chooses its file names, its
lines and its data, and none of them may fill the model's context or the investigation's cost.
"""

import io
import json
import re
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataproof_contracts import CheckOutcome, CheckResult, Environment, ErrorCode, typed_value
from dataproof_investigator.model import ToolSpec
from dataproof_verifier import (
    ALLOWED_FUNCTIONS,
    BindingRejectedError,
    QueryError,
    QueryLimits,
    check_read_query,
    run_read_only,
)

TOOL_VERSION = "tools/3"
MAX_SOURCE_LINES = 200
MAX_LINE_CHARS = 2000
"""A source line longer than this is cut, and marked as cut."""
MAX_QUERY_ROWS = 50
MAX_RESULT_BYTES = 64 * 1024
"""The largest result any tool returns."""
QUERY_TIMEOUT_MS = 5000
BRIEFING_EXAMPLES = 20
"""Mismatch examples of each failed check that the briefing shows: as many as a check records,
so that a second call for the rest is not needed."""
MAX_EXAMPLE_CHARS = 400
"""A value in a mismatch example longer than this is cut in the briefing."""
LISTED_FILES = 100
"""The most source files a briefing lists."""
LISTED_NAMES = 60
"""The most names an error lists."""
MAX_PATH_CHARS = 300
"""The longest path that can be read, and so the longest that is listed."""
_IDENTIFIER = r"^[a-z_][a-z0-9_]{0,62}$"
_CATALOG = re.compile(
    r"\b(information_schema|pg_catalog|pg_(class|tables|views|namespace|attribute|indexes"
    r"|constraint|proc|type|roles|user|authid|database|settings|stat_\w+))\b",
    re.IGNORECASE,
)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _BriefingArguments(_Arguments):
    """The briefing takes no arguments."""


class _CheckArguments(_Arguments):
    check_id: str = Field(min_length=1, max_length=128)


class _EvidenceArguments(_Arguments):
    evidence_id: str = Field(min_length=1, max_length=128)


class _SchemaArguments(_Arguments):
    environment: Literal["REFERENCE", "CANDIDATE"]
    table: str = Field(pattern=_IDENTIFIER)


class _SourceArguments(_Arguments):
    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class _QueryArguments(_Arguments):
    environment: Literal["REFERENCE", "CANDIDATE"]
    sql: str = Field(min_length=1, max_length=4000)
    max_rows: int = Field(ge=1, le=MAX_QUERY_ROWS)


_ARGUMENTS: dict[str, type[_Arguments]] = {
    "get_briefing": _BriefingArguments,
    "get_check_result": _CheckArguments,
    "get_evidence": _EvidenceArguments,
    "get_schema": _SchemaArguments,
    "read_source": _SourceArguments,
    "run_readonly_query": _QueryArguments,
}

_FUNCTIONS = ", ".join(sorted(ALLOWED_FUNCTIONS))
_CAST_ADVICE = (
    "A result can hold integers, numerics, text, booleans and timestamps with a time zone. "
    "Any other type (date, timestamp without time zone, which is also what AT TIME ZONE gives "
    "from a timestamp with one, float, interval, uuid, json, an array) must be cast to text "
    "in the query, for example created_on::text."
)
_QUERY_RULES = (
    "Rules: one statement. Application tables only: system catalogs (information_schema, "
    "pg_catalog) cannot be read, so use the briefing's table list and get_schema. Every output "
    "column needs its own name: alias duplicates, and do not use * over two tables that "
    f"share a column name. The only functions allowed are: {_FUNCTIONS}. Every built-in "
    "operator, CASE, COALESCE, FILTER, EXTRACT and AT TIME ZONE also work. So there is no "
    "array_agg, string_agg or to_jsonb: select the rows themselves, or count them. "
    f"{_CAST_ADVICE}"
)

_DESCRIPTIONS = {
    "get_briefing": "Return what this run already recorded, in one call: each failed or errored "
    f"check with its counts, its mismatch examples (up to {BRIEFING_EXAMPLES}) and the SQL of "
    "its evidence queries; the other checks' outcomes; the files the pull request changed, "
    "with their line counts; the readable source files; and the application tables of both "
    "databases. Call it first, on its own: it takes no arguments, the other tools need what it "
    "returns, and it replaces separate get_check_result and get_evidence calls. In a mismatch "
    "example, expected is the reference database's value and observed is the candidate's; a "
    "side that is null means the key is absent on that side.",
    "get_check_result": "Return one recorded check result in full, by its check ID. The "
    "briefing already holds each failed check's counts and mismatch examples; use this for a "
    "check that passed, or when the briefing says a value was cut.",
    "get_evidence": "Return one recorded evidence record of this run by its evidence ID. The "
    "briefing already holds every failed check's evidence queries; use this for an evidence ID "
    "the briefing does not show.",
    "get_schema": "Describe one application table of the reference or the candidate database: "
    "each column's type, precision, scale and nullability, and the table's constraints. The "
    "briefing lists the table names. Comparing a table's shape in the two databases shows a "
    "changed type or a lost constraint.",
    "read_source": "Read lines of one application or migration source file at the pull "
    f"request's head commit, at most {MAX_SOURCE_LINES} lines per call. path is the file's "
    "path from the repository root, exactly as the briefing lists it; the migration under "
    "review is among the briefing's changed files. A directory is not a file: asking for one "
    "returns the names inside it. The result gives each line as [line number, text], in "
    "order, and the file's total line count, so you can tell whether there is more to read.",
    "run_readonly_query": "Run one read-only SELECT (or WITH ... SELECT) against the reference "
    f"or the candidate database and return at most max_rows (up to {MAX_QUERY_ROWS}) rows. A "
    "query that returns more rows fails and returns nothing, so filter, aggregate or add "
    f"LIMIT. {_QUERY_RULES}",
}


def _schema(model: type[_Arguments]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.pop("description", None)
    schema.setdefault("properties", {})
    for prop in schema["properties"].values():
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
    changed_paths: tuple[str, ...] = ()
    """The paths the pull request changed: listed in the briefing and in the Change Analyst's
    task, so that neither role has to guess where the migration is."""


def _json(document: Any) -> str:
    return json.dumps(document, sort_keys=True, default=str)


# Source ----------------------------------------------------------------------------------------


def _readable(context: ToolContext) -> dict[str, str]:
    """The text of each regular file of the head commit under the readable roots.

    A name that occurs twice in the archive counts as its last member, which is the one an
    extraction would give; it is readable only if that member is a regular file. So a link
    that shares a file's name cannot be read through it.
    """
    if not context.source_archive:
        return {}
    texts: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(context.source_archive), mode="r:") as archive:
        last = {member.name: member for member in archive.getmembers()}
        for name, member in last.items():
            if not member.isfile() or not name.startswith(context.source_roots):
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                texts[name] = extracted.read().decode("utf-8", errors="replace")
    return texts


def _listable(name: str) -> bool:
    """Whether a path is shown in a listing: one that could be read, on one line."""
    return len(name) <= MAX_PATH_CHARS and name.isprintable()


def _inside(files: Sequence[str], directory: str) -> list[str]:
    """What a directory holds among the readable files: files, and subdirectories ending ``/``."""
    prefix = f"{directory}/"
    children = set()
    for name in files:
        if name.startswith(prefix):
            head, slash, _ = name[len(prefix) :].partition("/")
            children.add(prefix + head + slash)
    return sorted(children)


def _named(names: Sequence[str]) -> str:
    shown = ", ".join(names[:LISTED_NAMES])
    return shown + (f", and {len(names) - LISTED_NAMES} more" if len(names) > LISTED_NAMES else "")


def _source_lines(context: ToolContext, requested: str, start: int, end: int) -> str:
    # A trailing slash is how a directory is usually written; it is not a traversal.
    path = requested[:-1] if requested.endswith("/") and len(requested) > 1 else requested
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or ".." in normalized.parts or str(normalized) != path:
        raise ToolDenied(
            f"not a normalized relative path: {requested!r}. Give a file's path from the "
            "repository root, as the briefing lists it."
        )
    if end < start or end - start + 1 > MAX_SOURCE_LINES:
        raise ToolDenied(f"read at most {MAX_SOURCE_LINES} lines, with start <= end")
    texts = _readable(context)
    if path not in texts:
        raise _unreadable(context, sorted(texts), path)
    every = texts[path].splitlines()
    if start > len(every):
        # Nothing was read, so there is nothing to cite.
        raise ToolFailed(
            f"{path} has {len(every)} lines; start_line {start} is past its end. Read from line 1."
        )
    lines = [
        line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " [line cut here]"
        for line in every[start - 1 : end]
    ]
    # A list, in order: an object keyed by line number would be sorted as text (1, 10, 11, 2).
    return _json(
        {
            "path": path,
            "total_lines": len(every),
            "lines": [[start + i, line] for i, line in enumerate(lines)],
        }
    )


def _unreadable(context: ToolContext, files: Sequence[str], path: str) -> Exception:
    """Why ``path`` is not a readable file, and what is there instead.

    Only names under the readable roots are ever listed, and a path outside them gets the same
    answer whether or not it exists.
    """
    inside = [name for name in _inside(files, path) if _listable(name)]
    if inside:
        return ToolFailed(
            f"{path} is a directory, not a file. It holds: {_named(inside)}. Ask for a file "
            "by its full path."
        )
    if not path.startswith(context.source_roots):
        return ToolDenied(
            f"{path} is outside the readable source roots ({', '.join(context.source_roots)})"
        )
    for parent in map(str, PurePosixPath(path).parents):
        nearby = [name for name in _inside(files, parent) if _listable(name)]
        if nearby:
            return ToolFailed(
                f"{path} does not exist at the head commit. {parent} holds: {_named(nearby)}."
            )
    return ToolFailed(f"{path} does not exist at the head commit")


def _changed_files(context: ToolContext, texts: Mapping[str, str]) -> dict[str, Any]:
    """The readable files the pull request changed, with their line counts at the head, and
    how many are not listed (too many, or a name no call could read)."""
    changed = [p for p in context.changed_paths if p.startswith(context.source_roots)]
    shown = [path for path in changed if _listable(path)][:LISTED_FILES]
    listed: list[dict[str, Any]] = []
    for path in shown:
        if path in texts:
            listed.append({"path": path, "lines": len(texts[path].splitlines())})
        else:
            listed.append({"path": path, "at_head": False})
    return {"changed_files": listed, "changed_files_not_listed": len(changed) - len(shown)}


# Databases -------------------------------------------------------------------------------------

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
_TABLES_QUERY = """
SELECT rel.relname
FROM pg_class AS rel
JOIN pg_namespace AS ns ON ns.oid = rel.relnamespace
WHERE ns.nspname = 'public' AND rel.relkind IN ('r', 'p', 'v', 'm')
ORDER BY rel.relname
LIMIT 200
"""


def _connect(context: ToolContext, environment: str) -> psycopg.Connection[Any]:
    return psycopg.connect(context.databases[Environment(environment)], autocommit=True)


def _tables(context: ToolContext, environment: str) -> list[str]:
    """The application tables and views of one database; empty if it cannot be asked.

    A fixed, trusted catalog query: the model cannot read a catalog itself, so this is how it
    learns which tables there are.
    """
    if Environment(environment) not in context.databases:
        return []
    try:
        with _connect(context, environment) as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = 5000")  # QUERY_TIMEOUT_MS
            return [str(name) for (name,) in conn.execute(_TABLES_QUERY, prepare=True).fetchall()]
    except psycopg.Error:
        return []


def _its_tables(context: ToolContext, environment: str) -> str:
    tables = _tables(context, environment)
    if not tables:
        return ""
    return f" The {environment.lower()} database's tables are: {_named(tables)}."


_UNREACHABLE = "the {} database could not be asked just now. Try once more, or go on without it."
"""What a model is told when a database call fails outside the query itself. The driver's own
message is not passed on: it can name hosts, ports and roles."""


def _schema_of(context: ToolContext, environment: str, table: str) -> str:
    # Fixed, trusted catalog queries with the table name bound as a parameter.
    try:
        with _connect(context, environment) as conn, conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = 5000")  # QUERY_TIMEOUT_MS
            columns = conn.execute(_SCHEMA_QUERY, (table,), prepare=True).fetchall()
            constraints = conn.execute(_CONSTRAINT_QUERY, (table,), prepare=True).fetchall()
    except (psycopg.Error, KeyError) as exc:
        raise ToolFailed(_UNREACHABLE.format(environment.lower())) from exc
    if not columns:
        raise ToolFailed(
            f"no table {table} in the {environment.lower()} database."
            + _its_tables(context, environment)
        )
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


def _refusal(context: ToolContext, environment: str, sql: str, message: str) -> str:
    """A refused query's reason, with what to do instead."""
    reason = message.replace("in bindings", "in a diagnostic query").replace(
        "by a binding", "by a diagnostic query"
    )
    if _CATALOG.search(sql):
        return (
            f"{reason}. System catalogs cannot be read. To see a table's columns and "
            "constraints call get_schema." + _its_tables(context, environment)
        )
    if "function" in reason and "not allowed" in reason:
        return (
            f"{reason}. The only functions allowed are: {_FUNCTIONS}; built-in operators, CASE, "
            "COALESCE, FILTER, EXTRACT and AT TIME ZONE also work. Rewrite the query without "
            "that function: for example, select the rows themselves instead of collecting them."
        )
    if "specified more than once" in reason:
        return (
            f"{reason}. Every output column needs its own name: give duplicates an alias "
            "(AS), and do not use * over two tables that share a column name."
        )
    if "does not exist" in reason and "relation" in reason:
        return reason + "." + _its_tables(context, environment)
    if "does not exist" in reason and "column" in reason:
        return f"{reason}. A column name is wrong: get_schema lists a table's columns."
    return reason


def _query(context: ToolContext, environment: str, sql: str, max_rows: int) -> str:
    limits = QueryLimits(statement_timeout_ms=QUERY_TIMEOUT_MS, max_rows=max_rows)
    try:
        conn = _connect(context, environment)
    except (psycopg.Error, KeyError) as exc:
        raise ToolFailed(_UNREACHABLE.format(environment.lower())) from exc
    with conn:
        try:
            check_read_query(conn, sql, limits)
        except BindingRejectedError as exc:
            raise ToolDenied(_refusal(context, environment, sql, str(exc))) from exc
        except QueryError as exc:
            raise ToolFailed(str(exc)) from exc
        try:
            result = run_read_only(conn, sql, limits)
        except QueryError as exc:
            if exc.code is ErrorCode.RESULT_LIMIT_EXCEEDED:
                raise ToolFailed(
                    f"{exc.code.value}: the query returns more than {max_rows} rows, so nothing "
                    "is returned. Narrow it with WHERE, aggregate it (count, sum, min, max with "
                    f"GROUP BY), or add LIMIT; max_rows can be at most {MAX_QUERY_ROWS}."
                ) from exc
            if exc.code is ErrorCode.UNSUPPORTED_VALUE_TYPE:
                raise ToolFailed(_uncastable(str(exc))) from exc
            raise ToolFailed(f"{exc.code.value}: {exc}") from exc
    try:
        rows = [
            [typed_value(value).model_dump(mode="json") for value in row] for row in result.rows
        ]
    except (TypeError, ValueError) as exc:
        raise ToolFailed(_uncastable(str(exc))) from exc
    return _json({"environment": environment, "columns": list(result.columns), "rows": rows})


def _uncastable(reason: str) -> str:
    return (
        f"{ErrorCode.UNSUPPORTED_VALUE_TYPE.value}: {reason}. A selected column has a type "
        f"that cannot be returned as it is. {_CAST_ADVICE}"
    )


# The briefing ----------------------------------------------------------------------------------


def _briefed(context: ToolContext, check: CheckResult) -> dict[str, Any]:
    """One failed check as the briefing gives it: counts, a few examples, its evidence."""
    examples = [
        _cut(m.model_dump(mode="json")) for m in check.mismatch_examples[:BRIEFING_EXAMPLES]
    ]
    evidence = []
    for evidence_id in check.evidence_ids:
        record = context.evidence.get(evidence_id, {})
        shown = {k: v for k, v in record.items() if not k.endswith("sha256")}
        evidence.append({"evidence_id": evidence_id, **shown})
    return {
        "check_id": check.check_id,
        "outcome": check.outcome.value,
        "required": check.required,
        "error_code": check.error_code.value if check.error_code else None,
        "expected_key_count": check.expected_key_count,
        "observed_key_count": check.observed_key_count,
        "mismatch_count": check.mismatch_count,
        "mismatch_examples": examples,
        "mismatch_examples_recorded": len(check.mismatch_examples),
        "evidence": evidence,
    }


def _cut(document: Any) -> Any:
    """A recorded value with every long string cut, and marked as cut: a pull request's data
    chooses what a mismatch example holds."""
    if isinstance(document, str) and len(document) > MAX_EXAMPLE_CHARS:
        return document[:MAX_EXAMPLE_CHARS] + " [value cut here; get_check_result has all of it]"
    if isinstance(document, dict):
        return {key: _cut(value) for key, value in document.items()}
    if isinstance(document, list):
        return [_cut(value) for value in document]
    return document


def _briefing(context: ToolContext) -> str:
    """What the run holds before any other call. Before anything has run (the Change Analyst)
    there are no checks and no databases, and the briefing is the files alone."""
    texts = _readable(context)
    names = [name for name in sorted(texts) if _listable(name)]
    files: dict[str, Any] = _changed_files(context, texts) | {
        "source_files": names[:LISTED_FILES],
        "source_files_not_listed": len(texts) - len(names[:LISTED_FILES]),
    }
    if not context.checks and not context.databases:
        return _json(files)
    ordered = sorted(context.checks.values(), key=lambda c: (not c.required, c.check_id))
    failing = [c for c in ordered if c.outcome in (CheckOutcome.FAIL, CheckOutcome.ERROR)]
    tables = {
        environment.value: _tables(context, environment.value)
        for environment in (Environment.REFERENCE, Environment.CANDIDATE)
        if environment in context.databases
    }
    failed = [_briefed(context, check) for check in failing]
    rest = files | {
        "other_checks": {c.check_id: c.outcome.value for c in ordered if c not in failing},
        "tables": tables,
    }
    briefing = _json(rest | {"failed_checks": failed})
    if len(briefing.encode()) <= MAX_RESULT_BYTES:
        return briefing
    # Too large to send whole: the counts and the files stay, and the examples are a call away.
    for check in failed:
        check["mismatch_examples"] = "left out: the briefing was too large. Use get_check_result."
        check["evidence"] = [{"evidence_id": e["evidence_id"]} for e in check["evidence"]]
    return _json(rest | {"failed_checks": failed})


def run_tool(context: ToolContext, name: str, arguments: Mapping[str, Any]) -> str:
    """Execute one tool call and return its JSON result, or raise ToolDenied / ToolFailed.

    No result is larger than ``MAX_RESULT_BYTES``, whichever tool produced it.
    """
    result = _run(context, name, arguments)
    if len(result.encode()) > MAX_RESULT_BYTES:
        raise ToolFailed(
            f"the result is larger than {MAX_RESULT_BYTES} bytes, so nothing is returned. Ask "
            "for less: fewer lines, fewer columns or fewer rows."
        )
    return result


def _run(context: ToolContext, name: str, arguments: Mapping[str, Any]) -> str:
    model = _ARGUMENTS.get(name)
    if model is None:
        raise ToolDenied(f"there is no tool named {name!r}")
    try:
        parsed = model.model_validate(dict(arguments))
    except ValidationError as exc:
        raise ToolDenied(f"invalid arguments: {exc.errors()[0]['msg']}") from exc
    if isinstance(parsed, _BriefingArguments):
        return _briefing(context)
    if isinstance(parsed, _CheckArguments):
        check = context.checks.get(parsed.check_id)
        if check is None:
            raise ToolFailed(
                f"no check {parsed.check_id!r} in this run. Its checks are: "
                f"{_named(sorted(context.checks))}."
            )
        return check.model_dump_json()
    if isinstance(parsed, _EvidenceArguments):
        record = context.evidence.get(parsed.evidence_id)
        if record is None:
            raise ToolFailed(
                f"no evidence {parsed.evidence_id!r} in this run. Its evidence IDs are: "
                f"{_named(sorted(context.evidence))}."
            )
        return _json({"evidence_id": parsed.evidence_id, **record})
    if isinstance(parsed, _SchemaArguments):
        return _schema_of(context, parsed.environment, parsed.table)
    if isinstance(parsed, _SourceArguments):
        return _source_lines(context, parsed.path, parsed.start_line, parsed.end_line)
    if isinstance(parsed, _QueryArguments):
        return _query(context, parsed.environment, parsed.sql, parsed.max_rows)
    raise ToolDenied(f"there is no tool named {name!r}")  # pragma: no cover - exhaustive above
