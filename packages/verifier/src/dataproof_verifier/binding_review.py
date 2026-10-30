"""Binding statement validation and reviewer aids (ADR-0005), decided by PostgreSQL itself.

The binding is wrapped in ``CREATE TEMP VIEW`` inside a transaction that is always rolled back,
so nothing in it executes. PostgreSQL then guarantees it is one plain query (no writes, no
data-modifying ``WITH``, no second statement) and records what it uses:

- Relations come from ``pg_depend``, followed through every view reached, so wrapping a legacy
  table in a new view does not hide it.
- Functions, and every relation in a range table, also come from the analyzed rewrite trees of
  the binding and of every view reached. ``pg_depend`` alone is not enough: it omits built-in
  (pinned) objects, so system catalogs such as ``pg_class`` and calls such as
  ``query_to_xml('select * from legacy_payment_events', ...)``, which reads a table through SQL
  text, would be invisible there.

Only allowlisted built-in functions and built-in operators are accepted. User-defined functions
are rejected because their bodies hide relation reads, and system catalogs and foreign tables
are rejected outright. Each relation is then labelled by comparing the candidate catalog with
the reference catalog; relations the pull request did not create or alter are flagged.
"""

import re
from typing import Any

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from dataproof_contracts import (
    BindingAnalysis,
    BindingRelation,
    CandidateBinding,
    ErrorCode,
    RelationProvenance,
)
from dataproof_verifier.queries import DEFAULT_LIMITS, QueryError, QueryLimits

PROBE = "dataproof_binding_probe"

ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates and window functions
        "count", "sum", "min", "max", "avg", "bool_and", "bool_or", "every",
        "row_number", "rank", "dense_rank",
        # exact casts
        "numeric", "int2", "int4", "int8", "text", "varchar", "bpchar", "bool", "date",
        "timestamptz",
        # arithmetic, text and time helpers without side effects or hidden reads
        "abs", "round", "trunc", "ceil", "ceiling", "floor", "sign",
        "lower", "upper", "length", "char_length", "btrim", "ltrim", "rtrim", "substr",
        "substring", "date_trunc", "date_part", "extract", "timezone",
    }
)  # fmt: skip
"""Built-in functions a binding may call. Anything else fails closed."""

SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})
READABLE_RELKINDS = frozenset({"r", "p", "v", "m"})  # tables, partitioned tables, views
_FUNCTION_OID = re.compile(r":(?:funcid|aggfnoid|winfnoid|opfuncid) (\d+)")
_RELATION_OID = re.compile(r":relid (\d+)")

_REACHED = """
WITH RECURSIVE reached(oid, path) AS (
    SELECT d.refobjid, ARRAY[]::oid[]
    FROM pg_rewrite AS r
    JOIN pg_depend AS d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
    WHERE r.ev_class = %(probe)s::regclass AND d.deptype = 'n'
      AND d.refclassid = 'pg_class'::regclass AND d.refobjid <> r.ev_class
  UNION
    SELECT d.refobjid, reached.path || reached.oid
    FROM reached
    JOIN pg_class AS v ON v.oid = reached.oid AND v.relkind IN ('v', 'm')
    JOIN pg_rewrite AS r ON r.ev_class = v.oid
    JOIN pg_depend AS d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
    WHERE d.deptype = 'n' AND d.refclassid = 'pg_class'::regclass
      AND d.refobjid <> r.ev_class AND NOT d.refobjid = ANY(reached.path)
)
SELECT DISTINCT n.nspname || '.' || c.relname, n.nspname, c.relkind::text,
       ARRAY(SELECT pn.nspname || '.' || pc.relname
             FROM unnest(reached.path) WITH ORDINALITY AS p(oid, i)
             JOIN pg_class AS pc ON pc.oid = p.oid
             JOIN pg_namespace AS pn ON pn.oid = pc.relnamespace ORDER BY p.i),
       c.oid
FROM reached
JOIN pg_class AS c ON c.oid = reached.oid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
ORDER BY 1, 4
"""

_TREES = """
SELECT ev_action::text FROM pg_rewrite
WHERE ev_class = %(probe)s::regclass OR ev_class = ANY(%(views)s)
"""

_NON_BUILTIN_DEPENDENCIES = """
SELECT DISTINCT d.refclassid::regclass::text,
       CASE d.refclassid WHEN 'pg_proc'::regclass THEN d.refobjid::regprocedure::text
                         WHEN 'pg_operator'::regclass THEN d.refobjid::regoperator::text END
FROM pg_rewrite AS r
JOIN pg_depend AS d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
WHERE (r.ev_class = %(probe)s::regclass OR r.ev_class = ANY(%(views)s))
  AND d.refclassid IN ('pg_proc'::regclass, 'pg_operator'::regclass)
ORDER BY 1, 2
"""

_FUNCTIONS = """
SELECT p.oid::regprocedure::text, n.nspname, p.proname,
       EXISTS (SELECT 1 FROM pg_operator AS o
               WHERE o.oprcode = p.oid AND o.oprnamespace = 'pg_catalog'::regnamespace)
FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE p.oid = ANY(%(oids)s)
ORDER BY 1
"""

_TREE_RELATIONS = """
SELECT n.nspname || '.' || c.relname, n.nspname, c.relkind::text
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.oid = ANY(%(oids)s)
ORDER BY 1
"""

_FINGERPRINT = """
SELECT c.relkind::text,
       CASE WHEN c.relkind IN ('v', 'm') THEN pg_get_viewdef(c.oid) END,
       ARRAY(SELECT a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
                    || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
             FROM pg_attribute AS a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum)
FROM pg_class AS c WHERE c.oid = to_regclass(%(name)s)
"""


class BindingRejectedError(Exception):
    """Not an acceptable single read-only query: ``ERROR: BINDING_UNSUPPORTED_CONSTRUCT``."""


def _idle(connection: psycopg.Connection[Any]) -> None:
    if connection.info.transaction_status is not TransactionStatus.IDLE:
        raise QueryError(ErrorCode.QUERY_FAILED, "connection is not idle")


def _fingerprint(connection: psycopg.Connection[Any], name: str) -> tuple[object, ...] | None:
    row = connection.execute(_FINGERPRINT, {"name": name}).fetchone()
    return None if row is None else tuple(row)


def analyze_binding(
    binding: CandidateBinding,
    reference: psycopg.Connection[Any],
    candidate: psycopg.Connection[Any],
    limits: QueryLimits = DEFAULT_LIMITS,
) -> BindingAnalysis:
    """Validate a binding query and describe what it reads. Executes nothing from it."""
    if binding.query_sql is None:
        raise BindingRejectedError("binding has no query to analyze")
    _idle(reference)
    _idle(candidate)
    timeout = sql.SQL("SET LOCAL statement_timeout = {}").format(
        sql.Literal(limits.statement_timeout_ms)
    )
    try:
        with candidate.transaction(force_rollback=True):
            # The probe view is temporary and always rolled back, but creating it needs a
            # read-write transaction even for a role whose sessions default to read-only.
            # Privileges, not the transaction mode, keep this role from writing data.
            candidate.execute("SET TRANSACTION READ WRITE")
            candidate.execute(timeout)
            try:
                # One prepared statement: a second statement is a protocol error, not a command.
                # The binding is untrusted text embedded in DDL on purpose: PostgreSQL must parse
                # it as a view body, which only a single plain query can be.
                probe_ddl = f"CREATE TEMP VIEW {PROBE} AS {binding.query_sql}"
                candidate.execute(probe_ddl, prepare=True)
            except psycopg.Error as exc:
                message = str(exc).splitlines()[0]
                raise BindingRejectedError(f"not a single read-only query: {message}") from exc
            probe = f"pg_temp.{PROBE}"
            reached = candidate.execute(_REACHED, {"probe": probe}).fetchall()
            views = [oid for (_, _, relkind, _, oid) in reached if relkind in ("v", "m")]
            hidden = candidate.execute(
                _NON_BUILTIN_DEPENDENCIES, {"probe": probe, "views": views}
            ).fetchall()
            trees = candidate.execute(_TREES, {"probe": probe, "views": views}).fetchall()
            oids = sorted(
                {int(o) for (tree,) in trees for o in _FUNCTION_OID.findall(tree) if o != "0"}
            )
            functions = candidate.execute(_FUNCTIONS, {"oids": oids}).fetchall()
            relids = sorted(
                {int(o) for (tree,) in trees for o in _RELATION_OID.findall(tree)} - {0}
            )
            tree_relations = candidate.execute(_TREE_RELATIONS, {"oids": relids}).fetchall()
    except QueryError:
        raise
    except BindingRejectedError:
        raise
    except psycopg.Error as exc:
        raise QueryError(ErrorCode.QUERY_FAILED, str(exc).splitlines()[0]) from exc

    if hidden:
        names = ", ".join(name for _, name in hidden)
        raise BindingRejectedError(
            f"user-defined functions or operators hide what is read: {names}"
        )
    for signature, schema, name, is_operator in functions:
        if schema != "pg_catalog":
            raise BindingRejectedError(f"user-defined function {signature}")
        if name not in ALLOWED_FUNCTIONS and not is_operator:
            raise BindingRejectedError(f"function not allowed in bindings: {signature}")
    used = [(n, s, k) for n, s, k, _, _ in reached] + [tuple(row) for row in tree_relations]
    for name, schema, relkind in used:
        if schema in SYSTEM_SCHEMAS:
            raise BindingRejectedError(f"system relation {name} cannot be read by a binding")
        if relkind not in READABLE_RELKINDS:
            raise BindingRejectedError(f"relation {name} of kind {relkind!r} cannot be read")

    relations = []
    for name, _, _, through, _ in reached:
        before = _fingerprint(reference, name)
        after = _fingerprint(candidate, name)
        reference.rollback()
        candidate.rollback()
        if before is None:
            provenance = RelationProvenance.CREATED
        elif before != after:
            provenance = RelationProvenance.ALTERED
        else:
            provenance = RelationProvenance.UNCHANGED
        relations.append(
            BindingRelation(name=name, provenance=provenance, through_views=tuple(through))
        )
    return BindingAnalysis(
        binding_digest=binding.digest(),
        relations=tuple(relations),
        functions=tuple(signature for signature, *_ in functions),
    )
