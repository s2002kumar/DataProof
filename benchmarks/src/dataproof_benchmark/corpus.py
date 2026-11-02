"""The benchmark corpus: seeded fault and clean-control change sets over the commerce scenario.

Every case is an Alembic revision that a pull request could add, generated from a template and a
frozen seed (``CORPUS_SEED``), never tuned after the fact. What a case is (its family, whether it
is a fault, which contracts should fail, which event keys and columns it affects) is computed here
from the published fixture, independently of the SQL, and written only to the evaluator's label
manifest. Case and change-set names are neutral and shuffled, and every revision carries the same
docstring, so nothing the investigator can read names the fault (BENCHMARK, leakage rules).

Label provenance: generated from these templates by one author. The cases are **not**
independently authored.
"""

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

import commerce_scenario
from commerce_scenario import Fixture, build_fixture
from commerce_scenario.fixture import PaymentEvent

CORPUS_SEED = 20261102
"""Frozen before any evaluation. Changing it is a new corpus version."""
CORPUS_VERSION = 1
HOLDOUT_SHARE = 4
"""About one case in this many is held out, chosen by the seed."""

C1 = "commerce.c1_event_preservation"
C2 = "commerce.c2_refund_totals"
C3 = "commerce.c3_capture_totals"
C6_TOTALS = "commerce.c6_order_totals_api"
C6_REFUNDS = "commerce.c6_refund_events_api"
#: The ledger version of the sample app's payments API, which every case's pull request carries.
LEDGER_API = Path(commerce_scenario.__file__).resolve().parents[2] / "ledger-app" / "payments.py"
DOCSTRING = "Move legacy payment events into a per-order ledger."
CAPTURE, REFUND = "CAPTURE", "REFUND"
WITH_ORDERS = "legacy_payment_events AS e JOIN orders AS o ON o.order_id = e.order_id"

Kind = Literal["fault", "clean"]


@dataclass(frozen=True)
class Label:
    family: str
    kind: Kind
    variant: str
    covered: bool
    """Whether the declared contracts can observe the difference at all."""
    failing: tuple[str, ...]
    """Contracts expected to fail (or error)."""
    affected_events: tuple[int, ...]
    """C1 keys (event IDs) expected to differ, including keys that go missing or appear."""
    affected_columns: tuple[str, ...]
    """C1 columns expected to differ for keys on both sides (empty when keys go missing or
    appear)."""


@dataclass(frozen=True)
class Case:
    case_id: str
    change: str
    revision: str
    statements: tuple[str, ...]
    label: Label
    holdout: bool = False
    api_source: str | None = None
    """The payments API the pull request carries, when it is not the ledger version."""

    def source(self) -> str:
        """The revision file, as a pull request would add it."""
        body = "\n".join(
            f'    op.execute(\n        """{_sql(s)}"""\n    )' for s in self.statements
        )
        return (
            f'"""{DOCSTRING}\n\nRevision ID: {self.revision}\nRevises: 0001\n"""\n\n'
            "from alembic import op\n\n"
            f'revision = "{self.revision}"\n'
            'down_revision = "0001"\n'
            "branch_labels = None\n"
            "depends_on = None\n\n\n"
            f"def upgrade() -> None:\n{body}\n\n\n"
            "def downgrade() -> None:\n"
            '    op.execute("DROP TABLE IF EXISTS ledger_entries")\n'
        )


def _sql(statement: str) -> str:
    """A statement as it appears in the revision, without blank or trailing-space lines."""
    lines = [line.rstrip() for line in statement.splitlines() if line.strip()]
    if len(lines) == 1:
        return lines[0].strip()
    return "\n" + "\n".join(lines) + "\n        "


# SQL building blocks ------------------------------------------------------------------------------


def create_ledger(
    *,
    unique: bool = True,
    foreign_key: bool = True,
    amount: str = "numeric(18, 2)",
    extra: str = "",
) -> str:
    references = " REFERENCES orders (order_id)" if foreign_key else ""
    return f"""
        CREATE TABLE ledger_entries (
            order_id        bigint NOT NULL{references},
            ledger_sequence integer NOT NULL CHECK (ledger_sequence > 0),
            source_event_id bigint NOT NULL{" UNIQUE" if unique else ""},
            entry_kind      payment_event_kind NOT NULL,
            amount          {amount} NOT NULL CHECK (amount > 0),
            occurred_at     timestamptz NOT NULL,{extra}
            PRIMARY KEY (order_id, ledger_sequence)
        )
        """


def insert_ledger(
    *,
    amount: str = "e.amount",
    occurred: str = "e.occurred_at",
    source_id: str = "e.event_id",
    order: str = "e.occurred_at, e.event_id",
    sequence_offset: int = 0,
    source: str = "legacy_payment_events AS e",
    where: str = "",
    group: str = "",
    columns: str = "",
    values: str = "",
) -> str:
    offset = f"{sequence_offset} + " if sequence_offset else ""
    return f"""
        INSERT INTO ledger_entries
          (order_id, ledger_sequence, source_event_id, entry_kind, amount, occurred_at{columns})
        SELECT e.order_id,
               {offset}ROW_NUMBER() OVER (PARTITION BY e.order_id ORDER BY {order}),
               {source_id}, e.event_kind, {amount}, {occurred}{values}
        FROM {source}
        {where}
        {group}
        """


SAFE = (create_ledger(), insert_ledger())


# Fixture facts ------------------------------------------------------------------------------------


@dataclass
class Facts:
    fixture: Fixture
    events: dict[int, PaymentEvent] = field(default_factory=dict)
    by_order: dict[int, list[PaymentEvent]] = field(default_factory=lambda: defaultdict(list))
    items: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    currency: dict[int, str] = field(default_factory=dict)

    @classmethod
    def of(cls, fixture: Fixture) -> "Facts":
        facts = cls(fixture)
        for event in fixture.events:
            facts.events[event.event_id] = event
            facts.by_order[event.order_id].append(event)
        for item in fixture.items:
            facts.items[item.order_id] += 1
        for order in fixture.orders:
            facts.currency[order.order_id] = order.currency
        return facts

    def ids(self, predicate: Callable[[PaymentEvent], bool]) -> tuple[int, ...]:
        return tuple(sorted(e.event_id for e in self.events.values() if predicate(e)))

    def kinds(self, event_ids: Sequence[int]) -> set[str]:
        return {self.events[i].event_kind for i in event_ids}


def totals_failing(kinds: set[str]) -> tuple[str, ...]:
    """The total contracts that see a change to events of these kinds."""
    return tuple(c for c, k in ((C2, REFUND), (C3, CAPTURE)) if k in kinds)


def pair(
    rng: random.Random, facts: Facts, kind: str, *, equal: bool
) -> tuple[PaymentEvent, PaymentEvent]:
    """Two events of one order and kind, with equal (or different) amounts."""
    pairs = []
    for events in facts.by_order.values():
        same = sorted((e for e in events if e.event_kind == kind), key=lambda e: e.event_id)
        for i, first in enumerate(same):
            pairs += [(first, s) for s in same[i + 1 :] if (first.amount == s.amount) == equal]
    return rng.choice(pairs)


# Templates ----------------------------------------------------------------------------------------
# Each takes the case's own seeded generator, the fixture facts and a variant number.

Template = Callable[[random.Random, Facts, int], tuple[tuple[str, ...], Label]]


def join_multiplication(
    rng: random.Random, facts: Facts, variant: int
) -> tuple[tuple[str, ...], Label]:
    del rng
    joined = "legacy_payment_events AS e JOIN order_items AS i ON i.order_id = e.order_id"
    group = "GROUP BY e.event_id, e.order_id, e.event_kind, e.occurred_at"
    statements: tuple[str, ...]
    if variant == 0:
        name = "all events"
        statements = (
            create_ledger(),
            insert_ledger(amount="SUM(e.amount)", source=joined, group=group),
        )
        affected = facts.ids(lambda e: facts.items[e.order_id] > 1)
    else:
        name = "refunds only"
        statements = (
            create_ledger(),
            insert_ledger(where=f"WHERE e.event_kind = '{CAPTURE}'"),
            insert_ledger(
                amount="SUM(e.amount)",
                source=joined,
                where=f"WHERE e.event_kind = '{REFUND}'",
                group=group,
                sequence_offset=1000,
            ),
        )
        affected = facts.ids(lambda e: e.event_kind == REFUND and facts.items[e.order_id] > 1)
    failing = (C1, *totals_failing(facts.kinds(affected)))
    label = Label("join multiplication", "fault", name, True, failing, affected, ("amount",))
    return statements, label


def transposition(rng: random.Random, facts: Facts, variant: int) -> tuple[tuple[str, ...], Label]:
    kind = REFUND if variant == 0 else CAPTURE
    first, second = pair(rng, facts, kind, equal=False)
    swap = f"""
        UPDATE ledger_entries
        SET amount = CASE source_event_id WHEN {first.event_id} THEN {second.amount}
                                          ELSE {first.amount} END
        WHERE source_event_id IN ({first.event_id}, {second.event_id})
        """
    affected = (first.event_id, second.event_id)
    label = Label(
        "transposition", "fault", f"{kind.lower()} pair", True, (C1,), affected, ("amount",)
    )
    return (*SAFE, swap), label


def loss_and_duplication(
    rng: random.Random, facts: Facts, variant: int
) -> tuple[tuple[str, ...], Label]:
    kind = rng.choice([REFUND, CAPTURE])
    lost, kept = pair(rng, facts, kind, equal=variant == 0)
    statements = (
        create_ledger(unique=False),
        insert_ledger(),
        f"DELETE FROM ledger_entries WHERE source_event_id = {lost.event_id}",
        f"""
        INSERT INTO ledger_entries
          (order_id, ledger_sequence, source_event_id, entry_kind, amount, occurred_at)
        SELECT order_id, 1000, source_event_id, entry_kind, amount, occurred_at
        FROM ledger_entries WHERE source_event_id = {kept.event_id}
        """,
    )
    totals = () if lost.amount == kept.amount else totals_failing({kind})
    name = "equal amounts" if variant == 0 else "different amounts"
    affected = tuple(sorted((lost.event_id, kept.event_id)))
    label = Label("balanced loss and duplication", "fault", name, True, (C1, *totals), affected, ())
    return statements, label


def lossy_numeric(rng: random.Random, facts: Facts, variant: int) -> tuple[tuple[str, ...], Label]:
    del rng

    def rounded(event: PaymentEvent) -> bool:
        return event.amount.quantize(Decimal("0.1"), ROUND_HALF_UP) != event.amount

    def floored_eur(event: PaymentEvent) -> bool:
        floored = event.amount.quantize(Decimal("0.1"), ROUND_FLOOR)
        return facts.currency[event.order_id] == "EUR" and floored != event.amount

    if variant == 0:
        name = "one decimal place"
        statements = (create_ledger(amount="numeric(18, 1)"), insert_ledger())
        affected = facts.ids(rounded)
    else:
        name = "one currency floored"
        amount = "CASE WHEN o.currency = 'EUR' THEN floor(e.amount * 10) / 10 ELSE e.amount END"
        statements = (create_ledger(), insert_ledger(amount=amount, source=WITH_ORDERS))
        affected = facts.ids(floored_eur)
    failing = (C1, *totals_failing(facts.kinds(affected)))
    label = Label("lossy numeric conversion", "fault", name, True, failing, affected, ("amount",))
    return statements, label


def timestamp_shift(
    rng: random.Random, facts: Facts, variant: int
) -> tuple[tuple[str, ...], Label]:
    del rng
    if variant == 0:
        name = "local time read as UTC"
        occurred = "(e.occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/Toronto'"
        statements = (create_ledger(), insert_ledger(occurred=occurred))
        affected = facts.ids(lambda e: True)
    else:
        name = "one currency's zone"
        occurred = (
            "CASE WHEN o.currency = 'EUR' "
            "THEN (e.occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Paris' "
            "ELSE e.occurred_at END"
        )
        statements = (create_ledger(), insert_ledger(occurred=occurred, source=WITH_ORDERS))
        affected = facts.ids(lambda e: facts.currency[e.order_id] == "EUR")
    label = Label("timestamp shift", "fault", name, True, (C1,), affected, ("occurred_at",))
    return statements, label


def null_empty(rng: random.Random, facts: Facts, variant: int) -> tuple[tuple[str, ...], Label]:
    del rng, facts
    memo = "COALESCE(e.memo, '')" if variant == 0 else "NULLIF(e.memo, '')"
    statements = (
        create_ledger(extra="\n            memo            text,"),
        insert_ledger(columns=", memo", values=f", {memo}"),
    )
    name = "NULL to empty" if variant == 0 else "empty to NULL"
    # No declared contract reads the memo: an honest gap in coverage, kept in the corpus.
    return statements, Label("NULL and empty confusion", "fault", name, False, (), (), ())


def orphan_reference(
    rng: random.Random, facts: Facts, variant: int
) -> tuple[tuple[str, ...], Label]:
    kind = REFUND if variant == 0 else CAPTURE
    event = rng.choice(
        sorted((e for e in facts.events.values() if e.event_kind == kind), key=lambda e: e.event_id)
    )
    moved = (
        "UPDATE ledger_entries SET order_id = order_id + 1000000 "
        f"WHERE source_event_id = {event.event_id}"
    )
    statements = (create_ledger(foreign_key=False), insert_ledger(), moved)
    failing = (C1, *totals_failing({kind}))
    label = Label(
        "orphaned reference",
        "fault",
        f"one {kind.lower()}",
        True,
        failing,
        (event.event_id,),
        ("order_id",),
    )
    return statements, label


def wrong_mapping(rng: random.Random, facts: Facts, variant: int) -> tuple[tuple[str, ...], Label]:
    if variant == 0:
        captures = sorted(
            (e for e in facts.events.values() if e.event_kind == CAPTURE), key=lambda e: e.event_id
        )
        event = rng.choice(captures)
        relabel = (
            f"UPDATE ledger_entries SET entry_kind = '{REFUND}' "
            f"WHERE source_event_id = {event.event_id}"
        )
        label = Label(
            "wrong column mapping",
            "fault",
            "kind of one capture",
            True,
            (C1, C2, C3),
            (event.event_id,),
            ("event_kind",),
        )
        return (*SAFE, relabel), label
    placed = {o.order_id: o.placed_at for o in facts.fixture.orders}
    statements = (create_ledger(), insert_ledger(occurred="o.placed_at", source=WITH_ORDERS))
    affected = facts.ids(lambda e: e.occurred_at != placed[e.order_id])
    label = Label(
        "wrong column mapping",
        "fault",
        "time from the order",
        True,
        (C1,),
        affected,
        ("occurred_at",),
    )
    return statements, label


def partial_backfill(
    rng: random.Random, facts: Facts, variant: int
) -> tuple[tuple[str, ...], Label]:
    statements: tuple[str, ...]
    if variant == 0:
        name = "stops at a date"
        times = sorted({e.occurred_at for e in facts.events.values()})
        cutoff = times[int(len(times) * rng.uniform(0.85, 0.95))]
        where = f"WHERE e.occurred_at < '{cutoff.isoformat()}'"
        statements = (create_ledger(), insert_ledger(where=where))
        missing = facts.ids(lambda e: e.occurred_at >= cutoff)
    else:
        name = "batch boundary skipped"
        boundary = rng.choice(sorted(o for o in facts.by_order if 120 <= o <= 280))
        statements = (
            create_ledger(),
            insert_ledger(where=f"WHERE e.order_id < {boundary}"),
            insert_ledger(where=f"WHERE e.order_id > {boundary}"),
        )
        missing = facts.ids(lambda e: e.order_id == boundary)
    failing = (C1, *totals_failing(facts.kinds(missing)))
    return statements, Label("partial migration", "fault", name, True, failing, missing, ())


def retry_effects(rng: random.Random, facts: Facts, variant: int) -> tuple[tuple[str, ...], Label]:
    if variant == 0:
        event = rng.choice(sorted(facts.events.values(), key=lambda e: e.event_id))
        fix = (
            "UPDATE ledger_entries SET amount = amount + 0.01 "
            f"WHERE source_event_id = {event.event_id}"
        )
        failing = (C1, *totals_failing({event.event_kind}))
        label = Label(
            "retry effects",
            "fault",
            "fix-up applied twice",
            True,
            failing,
            (event.event_id,),
            ("amount",),
        )
        return (*SAFE, fix, fix), label
    again = insert_ledger(
        source_id="e.event_id + 1000000",
        sequence_offset=1000,
        where=f"WHERE e.event_kind = '{REFUND}'",
    )
    added = tuple(i + 1000000 for i in facts.ids(lambda e: e.event_kind == REFUND))
    label = Label("retry effects", "fault", "refunds inserted again", True, (C1, C2), added, ())
    return (*SAFE, again), label


CLEAN_VARIANTS: Sequence[tuple[str, tuple[str, ...]]] = (
    ("direct copy", SAFE),
    (
        "sequenced by reference",
        (create_ledger(), insert_ledger(order="e.occurred_at, e.external_ref")),
    ),
    ("legacy table retired", (*SAFE, "DROP TABLE legacy_payment_events")),
    (
        "staged copy",
        (
            "CREATE TABLE ledger_staging AS SELECT * FROM legacy_payment_events",
            create_ledger(),
            insert_ledger(source="ledger_staging AS e"),
            "DROP TABLE ledger_staging",
        ),
    ),
    ("wider precision", (create_ledger(amount="numeric(20, 4)"), insert_ledger())),
    (
        "timestamps through UTC",
        (
            create_ledger(),
            insert_ledger(occurred="(e.occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"),
        ),
    ),
    (
        "orders with and without items",
        (
            create_ledger(),
            insert_ledger(
                where="WHERE EXISTS (SELECT 1 FROM order_items AS i WHERE i.order_id = e.order_id)"
            ),
            insert_ledger(
                where="WHERE NOT EXISTS "
                "(SELECT 1 FROM order_items AS i WHERE i.order_id = e.order_id)"
            ),
        ),
    ),
    (
        "batched by order range",
        (
            create_ledger(),
            insert_ledger(where="WHERE e.order_id <= 150"),
            insert_ledger(where="WHERE e.order_id > 150"),
        ),
    ),
    (
        "distinct events",
        (
            create_ledger(),
            insert_ledger(
                source="(SELECT DISTINCT ON (event_id) * FROM legacy_payment_events "
                "ORDER BY event_id) AS e"
            ),
        ),
    ),
    (
        "extra column and index",
        (
            create_ledger(extra="\n            source_system   text NOT NULL DEFAULT 'legacy',"),
            insert_ledger(),
            "CREATE INDEX ledger_entries_occurred_at ON ledger_entries (occurred_at)",
        ),
    ),
    (
        "memo copied faithfully",
        (
            create_ledger(extra="\n            memo            text,"),
            insert_ledger(columns=", memo", values=", e.memo"),
        ),
    ),
)


def clean(rng: random.Random, facts: Facts, variant: int) -> tuple[tuple[str, ...], Label]:
    del rng, facts
    name, statements = CLEAN_VARIANTS[variant]
    return statements, Label("clean control", "clean", name, True, (), (), ())


FAULTS: Sequence[tuple[Template, int]] = (
    (join_multiplication, 2),
    (transposition, 2),
    (loss_and_duplication, 2),
    (lossy_numeric, 2),
    (timestamp_shift, 2),
    (null_empty, 2),
    (orphan_reference, 2),
    (wrong_mapping, 2),
    (partial_backfill, 2),
    (retry_effects, 2),
)


def build_corpus(seed: int = CORPUS_SEED) -> list[Case]:
    """Every case, in neutral shuffled order, with the frozen holdout marked."""
    facts = Facts.of(build_fixture())
    plans: list[tuple[Template, int]] = [
        (template, variant) for template, count in FAULTS for variant in range(count)
    ]
    plans += [(clean, variant) for variant in range(len(CLEAN_VARIANTS))]
    order = random.Random(seed)
    order.shuffle(plans)
    cases = []
    for index, (template, variant) in enumerate(plans):
        number = 101 + index
        statements, label = template(
            random.Random(f"{seed}:{template.__name__}:{variant}"), facts, variant
        )
        revision = f"{random.Random(f'{seed}:revision:{number}').getrandbits(48):012x}"
        cases.append(
            Case(
                case_id=f"case-{number}",
                change=f"ledger-backfill-{number}",
                revision=revision,
                statements=statements,
                label=label,
                holdout=order.randrange(HOLDOUT_SHARE) == 0,
            )
        )
    return cases


# API behaviour cases (pipeline tier) -------------------------------------------------------------
# The migration is the direct copy; the fault, if any, is in the API the pull request serves. Only
# the live previews can show these, so they run in the pipeline tier, not the database tier.


def _api_source(old: str, new: str) -> str:
    source = LEDGER_API.read_text("utf-8")
    if source.count(old) != 1:
        raise ValueError(f"the ledger API no longer contains {old!r}")
    return source.replace(old, new)


def api_variants(facts: Facts) -> Sequence[tuple[str, Label]]:
    captured_orders = sorted({e.order_id for e in facts.events.values() if e.event_kind == CAPTURE})
    rounded_refunds = facts.ids(
        lambda e: (
            e.event_kind == REFUND
            and e.amount.quantize(Decimal("0.1"), ROUND_HALF_EVEN) != e.amount
        )
    )
    return (
        (
            _api_source(
                "COALESCE(SUM(amount) FILTER (WHERE entry_kind = 'REFUND'), 0) AS refunded",
                "COALESCE(SUM(amount), 0) AS refunded",
            ),
            Label(
                "API behaviour",
                "fault",
                "refunded total over every entry",
                True,
                (C6_TOTALS,),
                tuple(captured_orders),
                ("refunded_total",),
            ),
        ),
        (
            _api_source(
                "    return [Transaction.model_validate(row) for row in rows]",
                "    return [\n"
                "        Transaction.model_validate(\n"
                '            {**row, "amount": Decimal(row["amount"]).quantize(Decimal("0.1"))}\n'
                "        )\n"
                "        for row in rows\n"
                "    ]",
            ),
            Label(
                "API behaviour",
                "fault",
                "transaction amounts rounded",
                True,
                (C6_REFUNDS,),
                rounded_refunds,
                ("amount",),
            ),
        ),
        (
            _api_source(
                "        SELECT COALESCE(SUM(amount) FILTER (WHERE entry_kind = 'CAPTURE'), 0)"
                " AS captured,\n"
                "               COALESCE(SUM(amount) FILTER (WHERE entry_kind = 'REFUND'), 0)"
                " AS refunded\n"
                "        FROM ledger_entries WHERE order_id = %s",
                "        SELECT (SELECT COALESCE(SUM(amount), 0) FROM ledger_entries\n"
                "                WHERE order_id = %s AND entry_kind = 'CAPTURE') AS captured,\n"
                "               (SELECT COALESCE(SUM(amount), 0) FROM ledger_entries\n"
                "                WHERE order_id = %s AND entry_kind = 'REFUND') AS refunded",
            ).replace(
                "        (order_id,),\n    ).fetchone()",
                "        (order_id, order_id),\n    ).fetchone()",
                1,
            ),
            Label("clean control", "clean", "totals from two subqueries", True, (), (), ()),
        ),
    )


def build_api_cases(seed: int = CORPUS_SEED) -> list[Case]:
    """The API behaviour cases, numbered from 201, in neutral shuffled order."""
    variants = list(api_variants(Facts.of(build_fixture())))
    random.Random(f"{seed}:api").shuffle(variants)
    cases = []
    for index, (api_source, label) in enumerate(variants):
        number = 201 + index
        revision = f"{random.Random(f'{seed}:revision:{number}').getrandbits(48):012x}"
        cases.append(
            Case(
                case_id=f"case-{number}",
                change=f"ledger-backfill-{number}",
                revision=revision,
                statements=SAFE,
                label=label,
                api_source=api_source,
            )
        )
    return cases
