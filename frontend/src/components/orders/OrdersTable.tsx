import { formatCountdown, formatPrice } from "../../lib/time";
import {
  getOrderMaxProfitUsdt,
  getOrderRecommendedProfitUsdt,
  getOrderTakeProfitDisplay,
  isClosedTrade,
  tradeTerminalTimestamp,
} from "../../lib/orders";
import { useAppStore } from "../../store/appStore";
import type { Order } from "../../types/api";
import { TableContainer } from "../layout/TableContainer";

type Props = {
  orders: Order[];
  onCancel?: (orderId: number) => void;
  showLifecycleColumns?: boolean;
  hideEmptyState?: boolean;
  emptyMessage?: string;
};

function formatOrderMoney(value: string | number | null | undefined): string {
  const numericValue = Number(value ?? 0);
  const prefix = numericValue > 0 ? "+" : numericValue < 0 ? "-" : "";
  return `${prefix}$${Math.abs(numericValue).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatProfitTarget(value: number): string {
  return `$${Math.max(value, 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatTimestamp(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "--";
}

function formatTakeProfitTargets(order: Order): string {
  const takeProfitDisplay = getOrderTakeProfitDisplay(order);
  if (!takeProfitDisplay) {
    return "--";
  }
  return takeProfitDisplay
    .split(" / ")
    .map((value) => formatPrice(value))
    .join(" / ");
}

function openedTimestamp(order: Order): string | null {
  return order.triggered_at ?? order.placed_at ?? order.created_at ?? null;
}

function finalResult(order: Order): string {
  if (!isClosedTrade(order)) {
    return "--";
  }
  if (order.status === "CLOSED_WIN") {
    return "WIN";
  }
  if (order.status === "CLOSED_LOSS") {
    return "LOSS";
  }
  if (order.status === "CLOSED_EXTERNALLY") {
    return "EXTERNAL";
  }
  return order.status;
}

export function OrdersTable({
  orders,
  onCancel,
  showLifecycleColumns = false,
  hideEmptyState = false,
  emptyMessage = "No orders yet.",
}: Props) {
  return (
    <div className="card table-card">
      <p className="muted">Each row is one setup for one coin. A live setup can create up to 4 Binance orders: entry, TP1, TP2, and stop loss.</p>
      <TableContainer>
        <table className={`data-table ${showLifecycleColumns ? "expansive-table" : "wide-table"}`}>
          <thead>
            <tr>
              {showLifecycleColumns ? <th>Order ID</th> : null}
              <th>Symbol</th>
              <th>Legs</th>
              <th>Direction</th>
              <th>Entry</th>
              <th>TP</th>
              <th>SL</th>
              <th>Recommended Profit</th>
              <th>Max Profit</th>
              <th>Risk $</th>
              <th>Risk %</th>
              <th>Source</th>
              <th>Status</th>
              <th>Expires</th>
              {showLifecycleColumns ? <th>Opened</th> : null}
              {showLifecycleColumns ? <th>Closed</th> : null}
              {showLifecycleColumns ? <th>Close Type</th> : null}
              {showLifecycleColumns ? <th>Result</th> : null}
              {showLifecycleColumns ? <th>Realized P&amp;L</th> : null}
              {onCancel ? <th /> : null}
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <OrderRow key={order.id} order={order} onCancel={onCancel} showLifecycleColumns={showLifecycleColumns} />
            ))}
            {orders.length === 0 && !hideEmptyState ? (
              <tr>
                <td colSpan={showLifecycleColumns ? (onCancel ? 19 : 18) : onCancel ? 14 : 13} className="empty-state">
                  {emptyMessage}
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </TableContainer>
    </div>
  );
}

type OrderRowProps = {
  order: Order;
  onCancel?: (orderId: number) => void;
  showLifecycleColumns: boolean;
};

function liveLegCount(order: Order): number {
  const protectedLegs = order.partial_tp_enabled
    ? [order.tp_order_1_id, order.tp_order_2_id, order.sl_order_id]
    : [order.tp_order_id, order.sl_order_id];
  return [order.entry_order_id, ...protectedLegs].filter(Boolean).length;
}

function orderLegCap(order: Order): number {
  return order.partial_tp_enabled ? 4 : 3;
}

function OrderRow({ order, onCancel, showLifecycleColumns }: OrderRowProps) {
  const isCancelling = useAppStore((state) => Boolean(state.pendingActions[`order:cancel:${order.id}`]));
  const riskPct = `${Number(order.risk_pct_of_wallet).toFixed(2)}%`;
  const recommendedProfit = getOrderRecommendedProfitUsdt(order);
  const maxProfit = getOrderMaxProfitUsdt(order);

  return (
    <tr>
      {showLifecycleColumns ? <td>{order.id}</td> : null}
      <td>{order.symbol}</td>
      <td>{liveLegCount(order)} / {orderLegCap(order)}</td>
      <td>{order.direction}</td>
      <td>{formatPrice(order.entry_price)}</td>
      <td>{formatTakeProfitTargets(order)}</td>
      <td>{formatPrice(order.stop_loss)}</td>
      <td>{formatProfitTarget(recommendedProfit)}</td>
      <td>{formatProfitTarget(maxProfit)}</td>
      <td>${formatPrice(order.risk_usdt_at_stop)}</td>
      <td>{riskPct}</td>
      <td>{order.approved_by}</td>
      <td className="cell-wrap-tight">{order.status}</td>
      <td>{formatCountdown(order.expires_at)}</td>
      {showLifecycleColumns ? <td>{formatTimestamp(openedTimestamp(order))}</td> : null}
      {showLifecycleColumns ? <td>{formatTimestamp(tradeTerminalTimestamp(order))}</td> : null}
      {showLifecycleColumns ? <td className="cell-wrap-tight">{order.close_type ?? "--"}</td> : null}
      {showLifecycleColumns ? <td className="cell-wrap-tight">{finalResult(order)}</td> : null}
      {showLifecycleColumns ? <td>{isClosedTrade(order) ? formatOrderMoney(order.realized_pnl) : "--"}</td> : null}
      {onCancel ? (
        <td className="table-action-cell">
          {order.status === "ORDER_PLACED" ? (
            <button className="secondary-button small" onClick={() => onCancel(order.id)} disabled={isCancelling}>
              {isCancelling ? "Cancelling..." : "Cancel"}
            </button>
          ) : null}
        </td>
      ) : null}
    </tr>
  );
}
