"""Orders and their line items."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from . import payments
from .db import Connection, connection
from .schemas import Item, OrderDetail, OrderSummary

router = APIRouter()
Conn = Annotated[Connection, Depends(connection)]


@router.get("/orders")
def list_orders(
    conn: Conn,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after: Annotated[int, Query(ge=0)] = 0,
) -> list[OrderSummary]:
    """Orders by ID, ``limit`` at a time, starting after the order ID ``after``."""
    rows = conn.execute(
        """
        SELECT order_id, customer_id, currency, status, placed_at FROM orders
        WHERE order_id > %s ORDER BY order_id LIMIT %s
        """,
        (after, limit),
    ).fetchall()
    return [OrderSummary.model_validate(row) for row in rows]


@router.get("/orders/{order_id}")
def order_detail(order_id: int, conn: Conn) -> OrderDetail:
    order = conn.execute(
        "SELECT order_id, customer_id, currency, status, placed_at FROM orders WHERE order_id = %s",
        (order_id,),
    ).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    items = conn.execute(
        "SELECT line_no, sku, unit_price, quantity FROM order_items "
        "WHERE order_id = %s ORDER BY line_no",
        (order_id,),
    ).fetchall()
    captured, refunded = payments.totals(conn, order_id)
    return OrderDetail(
        **order,
        items=[Item.model_validate(item) for item in items],
        captured_total=captured,
        refunded_total=refunded,
    )
