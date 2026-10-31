import { listOrders } from "@/lib/api";

// Rendered on request: static generation would call the API during the offline build.
export const dynamic = "force-dynamic";

export default async function OrdersPage({
  searchParams,
}: {
  searchParams: Promise<{ after?: string }>;
}) {
  const { after } = await searchParams;
  const start = Number.parseInt(after ?? "0", 10) || 0;
  const orders = await listOrders(start);
  const last = orders.at(-1);
  return (
    <>
      <h1>Orders</h1>
      <table>
        <thead>
          <tr>
            <th>Order</th>
            <th>Customer</th>
            <th>Status</th>
            <th>Currency</th>
            <th>Placed</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => (
            <tr key={order.order_id}>
              <td>
                <a href={`/orders/${order.order_id}`}>#{order.order_id}</a>
              </td>
              <td>{order.customer_id}</td>
              <td>{order.status}</td>
              <td>{order.currency}</td>
              <td>{order.placed_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {last ? <a href={`/?after=${last.order_id}`}>Next page</a> : <p>No more orders.</p>}
    </>
  );
}
