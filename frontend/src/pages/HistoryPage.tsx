import { useMemo } from "react";

import { ReadStatusNotice } from "../components/layout/ReadStatusNotice";
import { TableContainer } from "../components/layout/TableContainer";
import { summarizeClosedTrades, tradeTerminalTimestamp } from "../lib/orders";
import { formatDurationBetween, formatPrice } from "../lib/time";
import { useAppStore } from "../store/appStore";
import type { Order } from "../types/api";

function formatSignedMoney(value: string | number | null | undefined): string {
  const numericValue = Number(value ?? 0);
  const prefix = numericValue > 0 ? "+" : numericValue < 0 ? "-" : "";
  return `${prefix}$${Math.abs(numericValue).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatTradeResult(order: Order): string {
  if (order.status === "CLOSED_WIN") {
    return "WIN";
  }
  if (order.status === "CLOSED_LOSS") {
    return "LOSS";
  }
  if (order.status === "CLOSED_EXTERNALLY") {
    return "EXTERNAL";
  }
  if (order.status === "CLOSED_BY_BOT") {
    return "BOT";
  }
  return order.status;
}

export function HistoryPage() {
  const scanHistory = useAppStore((state) => state.scanHistory);
  const auditEntries = useAppStore((state) => state.auditEntries);
  const orders = useAppStore((state) => state.orders);
  const ordersReadStatus = useAppStore((state) => state.readStates.orders);
  const scanOverviewReadStatus = useAppStore((state) => state.readStates.scanOverview);
  const auditEntriesReadStatus = useAppStore((state) => state.readStates.auditEntries);
  const { closedTrades, realizedTotal, winningTrades, losingTrades } = useMemo(() => summarizeClosedTrades(orders), [orders]);
  const orderedClosedTrades = useMemo(
    () =>
      [...closedTrades].sort((left, right) => {
        const leftTime = new Date(tradeTerminalTimestamp(left) ?? 0).getTime();
        const rightTime = new Date(tradeTerminalTimestamp(right) ?? 0).getTime();
        return rightTime - leftTime;
      }),
    [closedTrades],
  );

  return (
    <div className="page-grid">
      <section className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Closed Positions</h2>
            <p className="muted">Only positions that actually opened and later closed are listed here with their realized outcome.</p>
          </div>
          <div className="section-head-meta">
            <p className="muted">
              {orderedClosedTrades.length} closed positions • {winningTrades} wins • {losingTrades} losses • {formatSignedMoney(realizedTotal)}
            </p>
          </div>
        </div>
        <ReadStatusNotice
          status={ordersReadStatus}
          unavailableMessage="Closed trade history could not be refreshed from the backend."
          staleMessage="Showing the last successful trade journal snapshot because the latest backend refresh failed."
        />
        <TableContainer>
          <table className="data-table expansive-table">
            <thead>
              <tr>
                <th>Order ID</th>
                <th>Symbol</th>
                <th>Direction</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Opened</th>
                <th>Closed</th>
                <th>Duration</th>
                <th>Result</th>
                <th>Close Type</th>
                <th>Realized P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {orderedClosedTrades.map((order) => {
                const closedAt = tradeTerminalTimestamp(order);
                return (
                  <tr key={order.id}>
                    <td>{order.id}</td>
                    <td>{order.symbol}</td>
                    <td>{order.direction}</td>
                    <td>{formatPrice(order.entry_price)}</td>
                    <td>{formatPrice(order.close_price)}</td>
                    <td>{order.triggered_at ? new Date(order.triggered_at).toLocaleString() : "--"}</td>
                    <td>{closedAt ? new Date(closedAt).toLocaleString() : "--"}</td>
                    <td>{formatDurationBetween(order.triggered_at, closedAt)}</td>
                    <td className="cell-wrap-tight">{formatTradeResult(order)}</td>
                    <td className="cell-wrap-tight">{order.close_type ?? "--"}</td>
                    <td>{formatSignedMoney(order.realized_pnl)}</td>
                  </tr>
                );
              })}
              {orderedClosedTrades.length === 0 && !ordersReadStatus.error ? (
                <tr>
                  <td colSpan={10} className="empty-state">
                    No closed positions yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </TableContainer>
      </section>

      <section className="card">
        <div className="section-head">
          <h2>Scan History</h2>
        </div>
        <ReadStatusNotice
          status={scanOverviewReadStatus}
          unavailableMessage="Scan history could not be refreshed from the backend."
          staleMessage="Showing the last successful scan history snapshot because the latest backend refresh failed."
        />
        <TableContainer>
          <table className="data-table wide-table">
            <thead>
              <tr>
                <th>Started</th>
                <th>Status</th>
                <th>Scanned</th>
                <th>Candidates</th>
                <th>Qualified</th>
              </tr>
            </thead>
            <tbody>
              {scanHistory.map((cycle) => (
                <tr key={cycle.id}>
                  <td>{new Date(cycle.started_at).toLocaleString()}</td>
                  <td className="cell-wrap-tight">{cycle.status}</td>
                  <td>{cycle.symbols_scanned}</td>
                  <td>{cycle.candidates_found}</td>
                  <td>{cycle.signals_qualified}</td>
                </tr>
              ))}
              {scanHistory.length === 0 && !scanOverviewReadStatus.error ? (
                <tr>
                  <td colSpan={5} className="empty-state">
                    No history yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </TableContainer>
      </section>

      <section className="card">
        <div className="section-head">
          <h2>Audit Log</h2>
        </div>
        <ReadStatusNotice
          status={auditEntriesReadStatus}
          unavailableMessage="Audit log entries could not be refreshed from the backend."
          staleMessage="Showing the last successful audit log snapshot because the latest backend refresh failed."
        />
        <TableContainer>
          <table className="data-table text-heavy-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Event</th>
                <th>Symbol</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {auditEntries.map((entry) => (
                <tr key={entry.id}>
                  <td>{new Date(entry.timestamp).toLocaleString()}</td>
                  <td className="cell-wrap-tight">{entry.event_type}</td>
                  <td>{entry.symbol ?? "-"}</td>
                  <td className="cell-wrap">{entry.message ?? "-"}</td>
                </tr>
              ))}
              {auditEntries.length === 0 && !auditEntriesReadStatus.error ? (
                <tr>
                  <td colSpan={4} className="empty-state">
                    No audit entries yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </TableContainer>
      </section>
    </div>
  );
}
