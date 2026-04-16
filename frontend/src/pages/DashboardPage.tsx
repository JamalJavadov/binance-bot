import type { ChangeEvent } from "react";

import { OrdersTable } from "../components/orders/OrdersTable";
import { isActiveOrder, summarizeClosedTrades } from "../lib/orders";
import { BestOrdersSection } from "../components/signals/BestOrdersSection";
import { ReadStatusNotice } from "../components/layout/ReadStatusNotice";
import { formatScanSource } from "../lib/scanLabels";
import { formatPrice } from "../lib/time";
import { TableContainer } from "../components/layout/TableContainer";
import { useAppStore } from "../store/appStore";
import type { ScanCycle, ScanSymbolResult, WorkflowEvent } from "../types/api";

function formatScanOption(cycle: ScanCycle): string {
  return `${new Date(cycle.started_at).toLocaleString()} • ${cycle.trigger_type} • ${cycle.status} • ${cycle.progress_pct}%`;
}

function formatResultNotes(result: ScanSymbolResult): string {
  const notes = [result.reason_text, ...result.filter_reasons, result.error_message].filter(Boolean);
  return notes.length > 0 ? notes.join(" • ") : "-";
}

function sortWorkflow(events: WorkflowEvent[]): WorkflowEvent[] {
  return [...events].sort((left, right) => {
    const leftTime = new Date(left.timestamp).getTime();
    const rightTime = new Date(right.timestamp).getTime();
    return leftTime === rightTime ? left.id - right.id : leftTime - rightTime;
  });
}

function sortResults(results: ScanSymbolResult[]): ScanSymbolResult[] {
  return [...results].sort((left, right) => {
    const leftFinal = left.final_score ?? -1;
    const rightFinal = right.final_score ?? -1;
    if (leftFinal !== rightFinal) {
      return rightFinal - leftFinal;
    }

    const leftConfirmation = left.confirmation_score ?? -1;
    const rightConfirmation = right.confirmation_score ?? -1;
    if (leftConfirmation !== rightConfirmation) {
      return rightConfirmation - leftConfirmation;
    }

    return left.symbol.localeCompare(right.symbol);
  });
}

function formatMoney(value: string | number | undefined | null): string {
  return Number(value ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatSignedMoney(value: string | number | undefined | null): string {
  const numericValue = Number(value ?? 0);
  const prefix = numericValue >= 0 ? "+" : "-";
  return `${prefix}$${formatMoney(Math.abs(numericValue))}`;
}

export function DashboardPage() {
  const latestScan = useAppStore((state) => state.latestScan);
  const portfolioSummary = useAppStore((state) => state.portfolioSummary);
  const positions = useAppStore((state) => state.positions);
  const scanHistory = useAppStore((state) => state.scanHistory);
  const selectedScanId = useAppStore((state) => state.selectedScanId);
  const selectedScanDetail = useAppStore((state) => state.selectedScanDetail);
  const statusOverviewReadStatus = useAppStore((state) => state.readStates.statusOverview);
  const scanOverviewReadStatus = useAppStore((state) => state.readStates.scanOverview);
  const selectedScanDetailReadStatus = useAppStore((state) => state.readStates.selectedScanDetail);
  const orders = useAppStore((state) => state.orders);
  const selectScan = useAppStore((state) => state.selectScan);
  const activeOrders = orders.filter(isActiveOrder);
  const { closedTrades, realizedTotal, winningTrades, losingTrades } = summarizeClosedTrades(orders);
  const selectedCycle = selectedScanDetail?.cycle ?? scanHistory.find((cycle) => cycle.id === selectedScanId) ?? null;
  const workflow = selectedScanDetail?.detail_available ? sortWorkflow(selectedScanDetail.workflow) : [];
  const scanResults = selectedScanDetail?.detail_available ? sortResults(selectedScanDetail.results) : [];
  const dashboardEmptyState = "No scan selected yet.";

  const handleSelectScan = (event: ChangeEvent<HTMLSelectElement>) => {
    const value = Number(event.target.value);
    if (!Number.isNaN(value)) {
      void selectScan(value);
    }
  };

  return (
    <div className="page-grid">
      <section className="card hero-card">
        <div>
          <p className="eyebrow">Auto Mode Analysis</p>
          <h2>Latest auto-managed market scan</h2>
          <p className="muted">
            Latest scan: {latestScan ? `${latestScan.status} (${latestScan.progress_pct}%)` : "No scans yet"}
            {latestScan ? ` • ${latestScan.trigger_type}` : ""}
          </p>
          {selectedCycle ? <p className="muted">Selected source: {formatScanSource(selectedCycle.trigger_type, null)}</p> : null}
          <p className="muted">New scans and trade openings are controlled only by Auto Mode.</p>
        </div>
      </section>

      <section className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Portfolio Monitor</h2>
            <p className="muted">AQRR tracks open P&amp;L here and can close managed positions early when the trade thesis or execution quality breaks.</p>
          </div>
          <div className="section-head-meta">
            <p className="muted">
              Last synced {portfolioSummary?.last_synced_at ? new Date(portfolioSummary.last_synced_at).toLocaleString() : "--"}
            </p>
          </div>
        </div>
        <div className="scan-summary-grid">
          <div className="summary-stat">
            <span className="label">Open Positions</span>
            <strong>{portfolioSummary?.open_position_count ?? 0}</strong>
          </div>
          <div className="summary-stat">
            <span className="label">Open P&amp;L</span>
            <strong>{formatSignedMoney(portfolioSummary?.total_unrealized_pnl)}</strong>
          </div>
          <div className="summary-stat">
            <span className="label">Winning</span>
            <strong>{portfolioSummary?.winning_position_count ?? 0}</strong>
          </div>
          <div className="summary-stat">
            <span className="label">Losing</span>
            <strong>{portfolioSummary?.losing_position_count ?? 0}</strong>
          </div>
        </div>
        <ReadStatusNotice
          status={statusOverviewReadStatus}
          unavailableMessage="Portfolio monitor data could not be refreshed from the backend."
          staleMessage="Showing the last successful portfolio snapshot because the latest backend refresh failed."
        />
        {positions.length === 0 ? (
          !statusOverviewReadStatus.error ? <div className="empty-state">No open Binance positions are being monitored right now.</div> : null
        ) : (
          <TableContainer>
            <table className="data-table wide-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Source</th>
                  <th>Direction</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Mark</th>
                  <th>Unrealized P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((position) => (
                  <tr key={`${position.symbol}-${position.position_side}`}>
                    <td>{position.symbol}</td>
                    <td className="cell-wrap-tight">{position.source_kind}</td>
                    <td>{position.direction}</td>
                    <td>{formatPrice(position.position_amount)}</td>
                    <td>{formatPrice(position.entry_price)}</td>
                    <td>{formatPrice(position.mark_price)}</td>
                    <td>{formatSignedMoney(position.unrealized_pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableContainer>
        )}
      </section>

      <section className="card">
        <div className="section-head">
          <div>
            <h2>Closed Bot Trades</h2>
            <p className="muted">Realized trade results are tracked separately from open-position P&amp;L.</p>
          </div>
        </div>
        <div className="scan-summary-grid">
          <div className="summary-stat">
            <span className="label">Realized P&amp;L</span>
            <strong>{formatSignedMoney(realizedTotal)}</strong>
          </div>
          <div className="summary-stat">
            <span className="label">Closed Trades</span>
            <strong>{closedTrades.length}</strong>
          </div>
          <div className="summary-stat">
            <span className="label">Winning</span>
            <strong>{winningTrades}</strong>
          </div>
          <div className="summary-stat">
            <span className="label">Losing</span>
            <strong>{losingTrades}</strong>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-head">
          <div>
            <h2>Selected Scan</h2>
            <p className="muted">Inspect one recent scan cycle, its workflow, and every scanned coin.</p>
          </div>
          <select
            className="panel-select"
            value={selectedScanId ?? ""}
            onChange={handleSelectScan}
            disabled={scanHistory.length === 0}
          >
            {scanHistory.length === 0 ? <option value="">No scans yet</option> : null}
            {scanHistory.map((cycle) => (
              <option key={cycle.id} value={cycle.id}>
                {formatScanOption(cycle)}
              </option>
            ))}
          </select>
        </div>
        <ReadStatusNotice
          status={scanOverviewReadStatus}
          unavailableMessage="Recent scan cycles could not be refreshed from the backend."
          staleMessage="Showing the last successful scan overview because the latest backend refresh failed."
        />

        {selectedCycle ? (
          <div className="scan-summary-grid">
            <div className="summary-stat">
              <span className="label">Started</span>
              <strong>{new Date(selectedCycle.started_at).toLocaleString()}</strong>
            </div>
            <div className="summary-stat">
              <span className="label">Status</span>
              <strong>{selectedCycle.status}</strong>
              <span className="muted">{selectedCycle.progress_pct}% complete</span>
            </div>
            <div className="summary-stat">
              <span className="label">Trigger</span>
              <strong>{selectedCycle.trigger_type}</strong>
            </div>
            <div className="summary-stat">
              <span className="label">Scanned</span>
              <strong>{selectedCycle.symbols_scanned}</strong>
            </div>
            <div className="summary-stat">
              <span className="label">Candidates</span>
              <strong>{selectedCycle.candidates_found}</strong>
            </div>
            <div className="summary-stat">
              <span className="label">Qualified</span>
              <strong>{selectedCycle.signals_qualified}</strong>
            </div>
          </div>
        ) : !scanOverviewReadStatus.error ? (
          <div className="empty-state">{dashboardEmptyState}</div>
        ) : null}

        <ReadStatusNotice
          status={selectedScanDetailReadStatus}
          unavailableMessage="The selected scan detail could not be refreshed from the backend."
          staleMessage="Showing the last successful scan detail because the latest backend refresh failed."
        />

        {selectedScanId != null && selectedScanDetail == null && selectedCycle != null && !selectedScanDetailReadStatus.error ? (
          <div className="notice-card">Loading scan detail...</div>
        ) : null}

        {selectedScanDetail != null && !selectedScanDetail.detail_available ? (
          <div className="notice-card">Full workflow and per-coin detail is only available for scans created after this update.</div>
        ) : null}
      </section>

      {selectedScanDetail?.detail_available ? (
        <section className="card">
          <div className="section-head">
            <h2>Workflow Timeline</h2>
            <div className="section-head-meta">
              <p className="muted">{workflow.length} events</p>
            </div>
          </div>
          {workflow.length === 0 ? (
            <div className="empty-state">Scan workflow will appear here as the selected cycle progresses.</div>
          ) : (
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
                  {workflow.map((entry) => (
                    <tr key={entry.id}>
                      <td>{new Date(entry.timestamp).toLocaleString()}</td>
                      <td className="cell-wrap-tight">{entry.event_type}</td>
                      <td>{entry.symbol ?? "-"}</td>
                      <td className="cell-wrap">{entry.message ?? "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableContainer>
          )}
        </section>
      ) : null}

      {selectedScanDetail?.detail_available ? (
        <section className="card">
          <div className="section-head">
            <h2>Scanned Coins</h2>
            <div className="section-head-meta">
              <p className="muted">{scanResults.length} rows</p>
            </div>
          </div>
          {scanResults.length === 0 ? (
            <div className="empty-state">Scan results will populate as symbols are processed.</div>
          ) : (
            <TableContainer>
              <table className="data-table text-heavy-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Outcome</th>
                    <th>Direction</th>
                    <th>Confirmation</th>
                    <th>Final</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {scanResults.map((result) => (
                    <tr key={`${result.symbol}-${result.outcome}-${result.direction ?? "NONE"}`}>
                      <td>{result.symbol}</td>
                      <td className="cell-wrap-tight">{result.outcome}</td>
                      <td>{result.direction ?? "-"}</td>
                      <td>{result.confirmation_score ?? "-"}</td>
                      <td>{result.final_score ?? "-"}</td>
                      <td className="cell-wrap">{formatResultNotes(result)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableContainer>
          )}
        </section>
      ) : null}

      <BestOrdersSection />

      <section>
        <div className="section-head">
          <div>
            <h2>Pending Orders</h2>
            <p className="muted">Three active setups can mean 9 Binance orders in total, because each setup manages entry, TP, and SL.</p>
          </div>
        </div>
        <OrdersTable orders={activeOrders} />
      </section>
    </div>
  );
}
