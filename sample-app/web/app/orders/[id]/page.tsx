import { notFound } from "next/navigation";

import { NotFoundError, getOrder } from "@/lib/api";
import { refundsFor } from "@/lib/payments";

export const dynamic = "force-dynamic";

export default async function OrderPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const orderId = Number.parseInt(id, 10);
  if (!Number.isSafeInteger(orderId) || orderId <= 0) {
    notFound();
  }
  let order;
  let refunds;
  try {
    [order, refunds] = await Promise.all([getOrder(orderId), refundsFor(orderId)]);
  } catch (error) {
    if (error instanceof NotFoundError) {
      notFound();
    }
    throw error;
  }
  return (
    <>
      <h1>Order #{order.order_id}</h1>
      <p>
        Customer {order.customer_id} · {order.status} · placed {order.placed_at}
      </p>
      <table>
        <thead>
          <tr>
            <th>Line</th>
            <th>SKU</th>
            <th className="money">Unit price</th>
            <th className="money">Quantity</th>
          </tr>
        </thead>
        <tbody>
          {order.items.map((item) => (
            <tr key={item.line_no}>
              <td>{item.line_no}</td>
              <td>{item.sku}</td>
              <td className="money">{item.unit_price}</td>
              <td className="money">{item.quantity}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table>
        <tbody>
          <tr>
            <th>Captured</th>
            <td className="money" data-testid="captured-total">
              {order.captured_total} {order.currency}
            </td>
          </tr>
          <tr>
            <th>Refunded</th>
            <td className="money" data-testid="refunded-total">
              {order.refunded_total} {order.currency}
            </td>
          </tr>
        </tbody>
      </table>
      <h2>Refunds</h2>
      {refunds.length === 0 ? (
        <p>No refunds.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th>Occurred</th>
              <th className="money">Amount</th>
            </tr>
          </thead>
          <tbody>
            {refunds.map((refund) => (
              <tr key={refund.eventId}>
                <td>{refund.eventId}</td>
                <td>{refund.occurredAt}</td>
                <td className="money">
                  {refund.amount} {order.currency}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
