"""Payments, read from ``legacy_payment_events`` (one row per capture or refund).

A change that normalizes payments into a ledger replaces this module.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from .db import Connection, connection
from .schemas import Refund

router = APIRouter()
Conn = Annotated[Connection, Depends(connection)]


def totals(conn: Connection, order_id: int) -> tuple[Decimal, Decimal]:
    """Captured and refunded totals of one order."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount) FILTER (WHERE event_kind = 'CAPTURE'), 0) AS captured,
               COALESCE(SUM(amount) FILTER (WHERE event_kind = 'REFUND'), 0) AS refunded
        FROM legacy_payment_events WHERE order_id = %s
        """,
        (order_id,),
    ).fetchone()
    if row is None:
        return Decimal("0.00"), Decimal("0.00")
    return Decimal(row["captured"]), Decimal(row["refunded"])


@router.get("/orders/{order_id}/refunds")
def refunds(order_id: int, conn: Conn) -> list[Refund]:
    if conn.execute("SELECT 1 FROM orders WHERE order_id = %s", (order_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="order not found")
    rows = conn.execute(
        """
        SELECT event_id, amount, occurred_at, external_ref FROM legacy_payment_events
        WHERE order_id = %s AND event_kind = 'REFUND'
        ORDER BY occurred_at, event_id
        """,
        (order_id,),
    ).fetchall()
    return [Refund.model_validate(row) for row in rows]
