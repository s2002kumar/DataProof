"""Read-only, bounded execution of contract queries.

Each query runs alone in a ``READ ONLY`` transaction with a statement timeout and a row cap. It
is sent as a prepared statement (extended protocol), so PostgreSQL rejects a string holding
several statements: under the simple protocol, ``SELECT 1; COMMIT; DELETE ...`` would end the
read-only transaction and run the ``DELETE``. A statement that ends the transaction itself or
returns no result set is refused.

This is the second layer: bindings are first validated by ``binding_review``. A least-privileged
database role in real runs (THREAT_MODEL, boundaries 3 and 4) arrives with the runner.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from pydantic import TypeAdapter

from dataproof_contracts import ErrorCode, TypedValue, typed_value

Row = tuple[object, ...]
_ROWS = TypeAdapter(list[list[TypedValue]])


@dataclass(frozen=True)
class QueryLimits:
    statement_timeout_ms: int = 10_000
    max_rows: int = 100_000


DEFAULT_LIMITS = QueryLimits()


class QueryError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    sql_sha256: str
    result_sha256: str


def sql_digest(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


def _result_digest(rows: tuple[Row, ...]) -> str:
    try:
        typed = [[typed_value(value) for value in row] for row in rows]
    except (TypeError, ValueError) as exc:
        raise QueryError(ErrorCode.UNSUPPORTED_VALUE_TYPE, str(exc)) from exc
    return hashlib.sha256(_ROWS.dump_json(typed)).hexdigest()


def _status(connection: psycopg.Connection[Any]) -> TransactionStatus:
    return connection.info.transaction_status


def run_read_only(
    connection: psycopg.Connection[Any], query: str, limits: QueryLimits = DEFAULT_LIMITS
) -> QueryResult:
    """Run one read-only statement. The connection must be idle (not inside a transaction)."""
    if _status(connection) is not TransactionStatus.IDLE:
        raise QueryError(ErrorCode.QUERY_FAILED, "connection is not idle")
    try:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(
                sql.SQL("SET LOCAL statement_timeout = {}").format(
                    sql.Literal(limits.statement_timeout_ms)
                )
            )
            # prepare=True forces the extended protocol: one statement per message.
            cursor = connection.execute(query, prepare=True)
            if _status(connection) is not TransactionStatus.INTRANS:
                raise QueryError(
                    ErrorCode.QUERY_FAILED, "statement ended the read-only transaction"
                )
            if cursor.description is None:
                raise QueryError(ErrorCode.QUERY_FAILED, "statement returned no result set")
            columns = tuple(column.name for column in cursor.description)
            rows = tuple(cursor.fetchmany(limits.max_rows + 1))
    except psycopg.Error as exc:
        raise QueryError(ErrorCode.QUERY_FAILED, str(exc).splitlines()[0]) from exc
    if len(rows) > limits.max_rows:
        raise QueryError(ErrorCode.RESULT_LIMIT_EXCEEDED, f"more than {limits.max_rows} rows")
    return QueryResult(columns, rows, sql_digest(query), _result_digest(rows))
