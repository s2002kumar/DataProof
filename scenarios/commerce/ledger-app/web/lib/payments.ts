// Refunds of an order, from the ledger API: the ledger change sets' pull requests carry this
// version of sample-app/web/lib/payments.ts, so their frontend must be rebuilt from source.

import { getJson, type Money } from "./api";

export interface RefundRow {
  eventId: number;
  amount: Money;
  occurredAt: string;
}

interface LedgerTransaction {
  source_event_id: number;
  ledger_sequence: number;
  kind: "CAPTURE" | "REFUND";
  amount: Money;
  occurred_at: string;
}

export async function refundsFor(orderId: number): Promise<RefundRow[]> {
  const refunds = await getJson<LedgerTransaction[]>(
    `/orders/${orderId}/transactions?kind=REFUND`,
  );
  return refunds.map((t) => ({
    eventId: t.source_event_id,
    amount: t.amount,
    occurredAt: t.occurred_at,
  }));
}
