// Server-side access to the commerce API. Pages call it at request time, never at build time.

export type Money = string; // exact decimal, as the API serializes it

export interface Item {
  line_no: number;
  sku: string;
  unit_price: Money;
  quantity: number;
}

export interface OrderSummary {
  order_id: number;
  customer_id: number;
  currency: string;
  status: string;
  placed_at: string;
}

export interface OrderDetail extends OrderSummary {
  items: Item[];
  captured_total: Money;
  refunded_total: Money;
}

export class NotFoundError extends Error {}

function apiBase(): string {
  return process.env.API_URL ?? "http://127.0.0.1:8000";
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, { cache: "no-store" });
  if (response.status === 404) {
    throw new NotFoundError(path);
  }
  if (!response.ok) {
    throw new Error(`${path}: HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export function listOrders(after: number, limit = 25): Promise<OrderSummary[]> {
  return getJson<OrderSummary[]>(`/orders?limit=${limit}&after=${after}`);
}

export function getOrder(orderId: number): Promise<OrderDetail> {
  return getJson<OrderDetail>(`/orders/${orderId}`);
}
