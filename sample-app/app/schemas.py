"""Response bodies. Money is exact (``Decimal``, serialized as a string); instants carry offsets."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class Item(BaseModel):
    line_no: int
    sku: str
    unit_price: Decimal
    quantity: int


class OrderSummary(BaseModel):
    order_id: int
    customer_id: int
    currency: str
    status: str
    placed_at: datetime


class OrderDetail(OrderSummary):
    items: list[Item]
    captured_total: Decimal
    refunded_total: Decimal


class Refund(BaseModel):
    event_id: int
    amount: Decimal
    occurred_at: datetime
    external_ref: str
