"""Payments, read from ``ledger_entries`` (one ledger row per source payment event).

The ledger change sets' pull requests replace ``sample-app/app/payments.py`` with this module:
order totals now come from the ledger, and ``GET /orders/{id}/refunds`` gives way to
``GET /orders/{id}/transactions?kind=REFUND`` (docs/SCENARIO_CONTRACT.md, C6).
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .db import Connection, connection

router = APIRouter()
Conn = Annotated[Connection, Depends(connection)]
Kind = Literal["CAPTURE", "REFUND"]


class Transaction(BaseModel):
    source_event_id: int
    ledger_sequence: int
    kind: Kind
    amount: Decimal
    occurred_at: datetime


def totals(conn: Connection, order_id: int) -> tuple[Decimal, Decimal]:
    """Captured and refunded totals of one order."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount) FILTER (WHERE entry_kind = 'CAPTURE'), 0) AS captured,
               COALESCE(SUM(amount) FILTER (WHERE entry_kind = 'REFUND'), 0) AS refunded
        FROM ledger_entries WHERE order_id = %s
        """,
        (order_id,),
    ).fetchone()
    if row is None:
        return Decimal("0.00"), Decimal("0.00")
    return Decimal(row["captured"]), Decimal(row["refunded"])


@router.get("/orders/{order_id}/transactions")
def transactions(
    order_id: int, conn: Conn, kind: Annotated[Kind | None, Query()] = None
) -> list[Transaction]:
    if conn.execute("SELECT 1 FROM orders WHERE order_id = %s", (order_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="order not found")
    rows = conn.execute(
        """
        SELECT source_event_id, ledger_sequence, entry_kind::text AS kind, amount, occurred_at
        FROM ledger_entries
        WHERE order_id = %s AND (%s::text IS NULL OR entry_kind::text = %s)
        ORDER BY ledger_sequence
        """,
        (order_id, kind, kind),
    ).fetchall()
    return [Transaction.model_validate(row) for row in rows]
