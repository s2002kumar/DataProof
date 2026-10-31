// Refunds of an order, from the legacy payments API. A ledger change replaces this module.

import { getJson, type Money } from "./api";

export interface RefundRow {
  eventId: number;
  amount: Money;
  occurredAt: string;
}

interface LegacyRefund {
  event_id: number;
  amount: Money;
  occurred_at: string;
  external_ref: string;
}

export async function refundsFor(orderId: number): Promise<RefundRow[]> {
  const refunds = await getJson<LegacyRefund[]>(`/orders/${orderId}/refunds`);
  return refunds.map((r) => ({ eventId: r.event_id, amount: r.amount, occurredAt: r.occurred_at }));
}
