import { useDeferredValue, useMemo, useState } from "react";

import { ReadStatusNotice } from "../components/layout/ReadStatusNotice";
import { OrdersTable } from "../components/orders/OrdersTable";
import { summarizeClosedTrades } from "../lib/orders";
import { useAppStore } from "../store/appStore";

function matchesOrderSearch(orderId: number, symbol: string, search: string): boolean {
  if (!search) {
    return true;
  }
  const normalizedSearch = search.trim().toLowerCase();
  return String(orderId).includes(normalizedSearch) || symbol.toLowerCase().includes(normalizedSearch);
}

export function OrdersPage() {
  const orders = useAppStore((state) => state.orders);
  const ordersReadStatus = useAppStore((state) => state.readStates.orders);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const filteredOrders = useMemo(
    () => orders.filter((order) => matchesOrderSearch(order.id, order.symbol, deferredSearch)),
    [deferredSearch, orders],
  );
  const { closedTrades } = useMemo(() => summarizeClosedTrades(orders), [orders]);

  return (
    <section className="page-grid">
      <div className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Orders</h2>
            <p className="muted">Search by canonical order ID or symbol. Closed trade details stay attached to the same order row.</p>
          </div>
          <div className="section-head-meta">
            <p className="muted">
              {filteredOrders.length} shown • {closedTrades.length} closed trades
            </p>
          </div>
        </div>
        <label className="field">
          <span>Search by Order ID or Symbol</span>
          <input
            type="search"
            placeholder="e.g. 142 or BTCUSDT"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <ReadStatusNotice
          status={ordersReadStatus}
          unavailableMessage="Orders could not be refreshed from the backend."
          staleMessage="Showing the last successful order snapshot because the latest backend refresh failed."
        />
      </div>
      <OrdersTable
        orders={filteredOrders}
        showLifecycleColumns
        hideEmptyState={Boolean(ordersReadStatus.error)}
        emptyMessage={search.trim() ? "No orders matched the current search." : "No orders yet."}
      />
    </section>
  );
}
