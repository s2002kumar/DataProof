"""Generate ``dataproof/fixture.sql``: a deterministic subscriptions-and-invoices dataset.

    python examples/subscriptions/scripts/make_fixture.py

The rows come from a fixed seed through plain Python, so a rerun writes the same file. The data
is fictional. It holds what a cents-to-money migration must get right: amounts whose cents are
not a whole number of currency units (1, 99, 1999, 123456), zero tax, billing periods that
cross daylight-saving transitions in several zones, paused and cancelled subscriptions, unpaid
invoices and invoices without an external reference.
"""

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20261104
OUT = Path(__file__).resolve().parents[1] / "dataproof" / "fixture.sql"

PLANS: list[tuple[object, ...]] = [
    (1, "starter-monthly", "USD", 1999, "MONTH"),
    (2, "team-monthly", "USD", 4900, "MONTH"),
    (3, "team-yearly", "EUR", 49999, "YEAR"),
    (4, "enterprise-yearly", "USD", 123456, "YEAR"),
    (5, "free", "USD", 0, "MONTH"),
]
ZONES = ["America/Toronto", "Europe/Berlin", "Asia/Kolkata", "Australia/Sydney", "UTC"]
#: Local midnights around daylight-saving transitions, so that periods cross them.
ANCHORS = [
    (2026, 3, 8),
    (2026, 3, 29),
    (2026, 10, 4),
    (2026, 10, 25),
    (2026, 11, 1),
    (2026, 1, 15),
    (2026, 6, 30),
]
ODD_CENTS = [1, 99, 100, 101, 1999, 2550, 123456, 0, 333, 1001]


def _sql(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _insert(table: str, columns: list[str], rows: Sequence[tuple[object, ...]]) -> str:
    lines = [f"INSERT INTO {table} ({', '.join(columns)}) VALUES"]
    lines += [
        "  (" + ", ".join(_sql(v) for v in row) + ")" + ("," if i < len(rows) - 1 else ";")
        for i, row in enumerate(rows)
    ]
    return "\n".join(lines)


def build() -> str:
    rng = random.Random(SEED)  # noqa: S311 - a seeded generator for reproducible test data
    customers: list[tuple[object, ...]] = []
    for customer_id in range(1, 31):
        zone = ZONES[(customer_id - 1) % len(ZONES)]
        created = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=rng.randint(0, 300))
        customers.append((customer_id, f"customer{customer_id}@example.test", zone, created))

    subscriptions: list[tuple[object, ...]] = []
    invoices: list[tuple[object, ...]] = []
    invoice_id = 5000
    for subscription_id in range(1001, 1046):
        customer_id = rng.randint(1, 30)
        tz = ZoneInfo(ZONES[(customer_id - 1) % len(ZONES)])
        plan = PLANS[rng.randrange(len(PLANS))]
        yearly = plan[4] == "YEAR"
        price = int(str(plan[3]))
        seats = rng.choice([1, 1, 2, 3, 5, 10, 25])
        discount_bp = rng.choice([0, 0, 0, 500, 1000, 1500, 10000])
        year, month, day = ANCHORS[rng.randrange(len(ANCHORS))]
        started = datetime(year, month, day, tzinfo=tz) - timedelta(days=rng.randint(0, 20))
        status = rng.choice(["ACTIVE", "ACTIVE", "ACTIVE", "PAUSED", "CANCELLED"])
        cancelled = (
            started + timedelta(days=rng.randint(30, 200)) if status == "CANCELLED" else None
        )
        subscriptions.append(
            (subscription_id, customer_id, plan[0], status, seats, discount_bp, started, cancelled)
        )
        periods = rng.randint(1, 6)
        period_start = started
        for _ in range(periods):
            length = timedelta(days=365 if yearly else 30)
            period_end = datetime(
                period_start.year + (1 if yearly else 0),
                period_start.month if yearly else period_start.month % 12 + 1,
                min(period_start.day, 28),
                period_start.hour,
                period_start.minute,
                tzinfo=tz,
            )
            if not yearly and period_start.month == 12:
                period_end = period_end.replace(year=period_start.year + 1)
            if period_end <= period_start:
                period_end = period_start + length
            if rng.random() < 0.3:
                subtotal = ODD_CENTS[rng.randrange(len(ODD_CENTS))]
            else:
                subtotal = price * seats * (10000 - discount_bp) // 10000
            tax = 0 if rng.random() < 0.25 else subtotal * 13 // 100
            invoice_id += 1
            issued = period_start + timedelta(hours=rng.randint(0, 48))
            paid = issued + timedelta(days=rng.randint(0, 20)) if rng.random() < 0.8 else None
            ref = f"inv_{invoice_id:06d}" if rng.random() < 0.9 else None
            invoices.append(
                (
                    invoice_id,
                    subscription_id,
                    period_start,
                    period_end,
                    subtotal,
                    tax,
                    subtotal + tax,
                    issued,
                    paid,
                    ref,
                )
            )
            period_start = period_end

    parts = [
        "-- Generated by examples/subscriptions/scripts/make_fixture.py; do not edit by hand.",
        f"-- Seed {SEED}. {len(customers)} customers, {len(subscriptions)} subscriptions, "
        f"{len(invoices)} invoices. Fictional data.",
        _insert("plans", ["plan_id", "code", "currency", "price_cents", "interval"], PLANS),
        _insert("customers", ["customer_id", "email", "time_zone", "created_at"], customers),
        _insert(
            "subscriptions",
            [
                "subscription_id",
                "customer_id",
                "plan_id",
                "status",
                "seats",
                "discount_bp",
                "started_at",
                "cancelled_at",
            ],
            subscriptions,
        ),
        _insert(
            "invoices",
            [
                "invoice_id",
                "subscription_id",
                "period_start",
                "period_end",
                "subtotal_cents",
                "tax_cents",
                "total_cents",
                "issued_at",
                "paid_at",
                "external_ref",
            ],
            invoices,
        ),
    ]
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    OUT.write_text(build(), "utf-8", newline="\n")
    print(f"wrote {OUT}")
