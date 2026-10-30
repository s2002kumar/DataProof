"""ADR-0005 review aids and binding statement validation, against real PostgreSQL.

Attacks are staged as a pull request would stage them: as DDL on the candidate database (the
PR's migrations), followed by a binding that reads through what that DDL created.
"""

import re
from collections.abc import Iterator

import psycopg
import pytest

from commerce_env import (
    C1,
    SAFE_CHANGE,
    DatabaseFactory,
    baseline,
    candidate,
    contracts,
    database_factory,
    override,
    scalar,
    verify,
)
from dataproof_contracts import (
    BindingAnalysis,
    BindingRelation,
    CandidateBinding,
    CheckOutcome,
    ErrorCode,
    NotRunReason,
    RelationProvenance,
)
from dataproof_verifier import BindingRejectedError, analyze_binding

pytestmark = pytest.mark.integration

CREATED, ALTERED, UNCHANGED = (
    RelationProvenance.CREATED,
    RelationProvenance.ALTERED,
    RelationProvenance.UNCHANGED,
)
LEDGER_C1 = (
    "SELECT source_event_id, order_id, entry_kind::text AS entry_kind, amount, occurred_at"
    " FROM ledger_entries"
)
LEDGER_C2 = """
SELECT o.order_id, o.currency::text AS currency,
       COALESCE(SUM(l.amount) FILTER (WHERE l.entry_kind = 'REFUND'), 0)::numeric(18, 2)
         AS refund_total
FROM orders AS o LEFT JOIN ledger_entries AS l ON l.order_id = o.order_id
GROUP BY o.order_id, o.currency
"""


@pytest.fixture(scope="module")
def envs(postgres_dsn: str) -> Iterator[dict[str, str]]:
    with database_factory(postgres_dsn) as create:
        yield {"reference": baseline(create), "safe": candidate(create, SAFE_CHANGE)}


def binding(query_sql: str) -> CandidateBinding:
    return CandidateBinding(
        source_path="scenarios/commerce/bindings/probe.toml",
        head_sha="c" * 40,
        query_sql=query_sql,
        column_mapping={"event_id": "source_event_id"},
    )


def analyze(reference: str, candidate_dsn: str, query_sql: str) -> BindingAnalysis:
    with (
        psycopg.connect(reference, autocommit=True) as ref,
        psycopg.connect(candidate_dsn, autocommit=True) as cand,
    ):
        return analyze_binding(binding(query_sql), ref, cand)


def staged(new_database: DatabaseFactory, *ddl: str) -> str:
    """A candidate whose pull request also ran ``ddl`` in its migrations."""
    dsn = candidate(new_database, SAFE_CHANGE)
    with psycopg.connect(dsn) as connection:
        for statement in ddl:
            connection.execute(statement)
    return dsn


def relations(analysis: BindingAnalysis) -> list[tuple[str, RelationProvenance, tuple[str, ...]]]:
    return [(r.name, r.provenance, r.through_views) for r in analysis.relations]


def test_binding_reading_only_the_new_ledger_is_not_flagged(envs: dict[str, str]) -> None:
    analysis = analyze(envs["reference"], envs["safe"], LEDGER_C1)
    assert relations(analysis) == [("public.ledger_entries", CREATED, ())]
    assert analysis.flagged() == ()
    assert analysis.binding_digest == binding(LEDGER_C1).digest()


def test_joining_unaltered_orders_is_flagged_but_allowed(envs: dict[str, str]) -> None:
    analysis = analyze(envs["reference"], envs["safe"], LEDGER_C2)
    assert relations(analysis) == [
        ("public.ledger_entries", CREATED, ()),
        ("public.orders", UNCHANGED, ()),
    ]
    assert [r.name for r in analysis.flagged()] == ["public.orders"]
    assert "sum(numeric)" in analysis.functions


def test_relation_altered_by_the_pr_is_labelled_altered(
    new_database: DatabaseFactory, envs: dict[str, str]
) -> None:
    target = staged(new_database, "ALTER TABLE orders ADD COLUMN channel text")
    analysis = analyze(envs["reference"], target, LEDGER_C2)
    assert ("public.orders", ALTERED, ()) in relations(analysis)
    assert analysis.flagged() == ()


def test_reading_the_legacy_table_directly_is_flagged(envs: dict[str, str]) -> None:
    analysis = analyze(
        envs["reference"],
        envs["safe"],
        "SELECT event_id AS source_event_id, amount FROM legacy_payment_events",
    )
    assert analysis.flagged() == (
        BindingRelation(name="public.legacy_payment_events", provenance=UNCHANGED),
    )


def test_a_new_wrapper_view_does_not_hide_the_legacy_table(
    new_database: DatabaseFactory, envs: dict[str, str]
) -> None:
    target = staged(
        new_database,
        "CREATE VIEW ledger_view AS"
        " SELECT event_id AS source_event_id, amount FROM legacy_payment_events",
    )
    analysis = analyze(envs["reference"], target, "SELECT source_event_id, amount FROM ledger_view")
    assert relations(analysis) == [
        ("public.ledger_view", CREATED, ()),
        ("public.legacy_payment_events", UNCHANGED, ("public.ledger_view",)),
    ]
    assert [r.name for r in analysis.flagged()] == ["public.legacy_payment_events"]


@pytest.mark.parametrize(
    ("ddl", "query_sql", "message"),
    [
        (
            "CREATE FUNCTION hidden_amount(bigint) RETURNS numeric LANGUAGE sql"
            " AS 'SELECT amount FROM legacy_payment_events WHERE event_id = $1'",
            "SELECT source_event_id, hidden_amount(source_event_id) AS amount FROM ledger_entries",
            "hidden_amount(bigint)",
        ),
        (
            "CREATE FUNCTION same_id(bigint, bigint) RETURNS boolean LANGUAGE sql"
            " AS 'SELECT $1 = $2';"
            " CREATE OPERATOR === (LEFTARG = bigint, RIGHTARG = bigint, FUNCTION = same_id)",
            "SELECT source_event_id, amount FROM ledger_entries"
            " WHERE source_event_id === source_event_id",
            "===(bigint,bigint)",
        ),
        (
            "CREATE VIEW sneaky AS SELECT query_to_xml("
            "'select * from legacy_payment_events', true, true, '') AS payload",
            "SELECT payload FROM sneaky",
            "query_to_xml",
        ),
    ],
    ids=["user-defined-function", "user-defined-operator", "view-laundering-query_to_xml"],
)
def test_hidden_reads_are_rejected(
    new_database: DatabaseFactory, envs: dict[str, str], ddl: str, query_sql: str, message: str
) -> None:
    target = staged(new_database, *ddl.split("; "))
    with pytest.raises(BindingRejectedError, match=re.escape(message)):
        analyze(envs["reference"], target, query_sql)


@pytest.mark.parametrize(
    ("query_sql", "message"),
    [
        ("SELECT query_to_xml('select 1', true, true, '') AS x", "query_to_xml"),
        ("SELECT 1 AS one WHERE pg_sleep(0.1) IS NOT NULL", "not allowed"),
        ("SELECT now() AS at", "now()"),
        ("SELECT relname FROM pg_catalog.pg_class", "system relation"),
        ("DELETE FROM ledger_entries RETURNING *", "not a single read-only query"),
        (f"{LEDGER_C1}; DROP TABLE ledger_entries", "not a single read-only query"),
    ],
)
def test_unsupported_constructs_are_rejected_and_nothing_runs(
    envs: dict[str, str], query_sql: str, message: str
) -> None:
    before = scalar(envs["safe"], "SELECT count(*) FROM ledger_entries")
    with pytest.raises(BindingRejectedError, match=re.escape(message)):
        analyze(envs["reference"], envs["safe"], query_sql)
    assert scalar(envs["safe"], "SELECT count(*) FROM ledger_entries") == before > 0


def test_the_probe_view_never_survives(envs: dict[str, str]) -> None:
    with (
        psycopg.connect(envs["reference"], autocommit=True) as ref,
        psycopg.connect(envs["safe"], autocommit=True) as cand,
    ):
        analyze_binding(binding(LEDGER_C1), ref, cand)
        row = cand.execute("SELECT to_regclass('pg_temp.dataproof_binding_probe')").fetchone()
        assert row == (None,)


def test_reviewer_sees_the_problem_before_approving(
    new_database: DatabaseFactory, envs: dict[str, str]
) -> None:
    target = staged(
        new_database,
        "CREATE FUNCTION hidden_amount(bigint) RETURNS numeric LANGUAGE sql"
        " AS 'SELECT amount FROM legacy_payment_events WHERE event_id = $1'",
    )
    sneaky = (
        "SELECT source_event_id, order_id, entry_kind::text AS entry_kind,"
        " hidden_amount(source_event_id) AS amount, occurred_at FROM ledger_entries"
    )
    pending = verify(
        contracts(SAFE_CHANGE, approve=False, overrides=override(C1, sneaky)),
        envs["reference"],
        target,
    )[C1]
    assert pending.check.not_run_reason is NotRunReason.AWAITING_BINDING_REVIEW
    assert pending.binding_problem is not None
    assert "hidden_amount" in pending.binding_problem

    approved = verify(
        contracts(SAFE_CHANGE, overrides=override(C1, sneaky)), envs["reference"], target
    )[C1]
    assert (approved.check.outcome, approved.check.error_code) == (
        CheckOutcome.ERROR,
        ErrorCode.BINDING_UNSUPPORTED_CONSTRUCT,
    )
