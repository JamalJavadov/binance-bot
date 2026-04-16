import { useMemo } from "react";

import { ReadStatusNotice } from "../components/layout/ReadStatusNotice";
import { TableContainer } from "../components/layout/TableContainer";
import { isActiveOrder, isClosedTrade, tradeTerminalTimestamp } from "../lib/orders";
import { OrdersTable } from "../components/orders/OrdersTable";
import { useAppStore } from "../store/appStore";
import type { Order, ScanCycleDetail, ScanSymbolResult } from "../types/api";

const SHARED_ENTRY_SLOT_CAP = 3;

const BLOCKER_LABELS: Record<string, string> = {
  entry_too_far_from_mark: "Entry too far from mark",
  entry_crossed: "Entry already crossed",
  stop_loss_crossed: "Stop-loss already crossed",
  take_profit_crossed: "Take-profit already crossed",
  quote_volume_below_liquidity_floor: "Liquidity floor failed",
  spread_above_threshold: "Spread above AQRR cap",
  spread_relative_above_threshold: "Relative spread above AQRR cap",
  spread_unavailable: "Spread unavailable",
  order_book_unstable: "Order book unstable",
  execution_tier_c_rejected: "Tier C execution rejected",
  unstable_no_trade: "Unstable market state",
  no_aqrr_setup: "No AQRR setup",
  aqrr_hard_filters_failed: "AQRR hard filters failed",
  preview_not_placeable: "Order preview not placeable",
  insufficient_closed_candles: "Insufficient candle history",
  slot_limit_reached: "Selection slot limit reached",
  correlation_conflict: "Correlation conflict",
  btc_beta_conflict: "BTC beta conflict",
  cluster_conflict: "Cluster concentration conflict",
};

type BlockerSummary = {
  key: string;
  label: string;
  count: number;
};

function formatMoney(value: string | number | undefined | null): string {
  return Number(value ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatTimestamp(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "--";
}

function isAutoModeAudit(details: Record<string, unknown>, eventType: string): boolean {
  const reason = typeof details.reason === "string" ? details.reason : "";
  const approvedBy = typeof details.approved_by === "string" ? details.approved_by : "";
  return (
    eventType.startsWith("AUTO_MODE") ||
    eventType === "ORDER_CLOSED_BY_BOT" ||
    approvedBy === "AUTO_MODE" ||
    reason.startsWith("auto_mode")
  );
}

function joinFilterReasons(result: Pick<ScanSymbolResult, "filter_reasons">): string {
  return result.filter_reasons
    .map((reason) => BLOCKER_LABELS[reason] ?? reason)
    .join(", ");
}

function summarizeBlockers(detail?: ScanCycleDetail | null): BlockerSummary[] {
  if (detail == null) {
    return [];
  }

  const counts = new Map<string, number>();
  for (const result of detail.results) {
    if (result.outcome !== "FILTERED_OUT") {
      continue;
    }
    for (const reason of result.filter_reasons) {
      const label = BLOCKER_LABELS[reason];
      if (label == null) {
        continue;
      }
      counts.set(reason, (counts.get(reason) ?? 0) + 1);
    }
  }

  return Array.from(counts.entries())
    .map(([key, count]) => ({
      key,
      label: BLOCKER_LABELS[key] ?? key,
      count,
    }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

function buildDiagnosticsConclusion({
  blockerSummaries,
  remainingEntrySlots,
}: {
  blockerSummaries: BlockerSummary[];
  remainingEntrySlots: number;
}): string {
  if (remainingEntrySlots <= 0) {
    return "New orders are currently capped by shared slot usage, so the latest cycle cannot open another setup until a slot frees up.";
  }
  if (blockerSummaries.length === 0) {
    return "The latest AQRR cycle did not surface filtered blocker counts, so there may simply have been no viable setups to rank into the top selection pool.";
  }
  return "The latest AQRR cycle rejected setups because they failed execution quality, regime clarity, net-3R feasibility, or diversification controls.";
}

export function AutoModePage() {
  const autoMode = useAppStore((state) => state.autoMode);
  const orders = useAppStore((state) => state.orders);
  const auditEntries = useAppStore((state) => state.auditEntries);
  const latestAutoModeScanDetail = useAppStore((state) => state.latestAutoModeScanDetail);
  const autoModeReadStatus = useAppStore((state) => state.readStates.autoMode);
  const ordersReadStatus = useAppStore((state) => state.readStates.orders);
  const auditEntriesReadStatus = useAppStore((state) => state.readStates.auditEntries);
  const latestAutoModeScanDetailReadStatus = useAppStore((state) => state.readStates.latestAutoModeScanDetail);
  const setAutoModeEnabled = useAppStore((state) => state.setAutoModeEnabled);
  const setAutoModePaused = useAppStore((state) => state.setAutoModePaused);
  const isToggling = useAppStore((state) => Boolean(state.pendingActions["auto-mode:toggle"]));
  const isPausePending = useAppStore((state) => Boolean(state.pendingActions["auto-mode:pause"]));

  const isEnabled = autoMode?.enabled ?? false;
  const isPaused = autoMode?.paused ?? false;
  const statusLabel = autoMode?.running ? "Running" : isEnabled ? (isPaused ? "Paused" : "Enabled") : "Disabled";
  const toggleButtonLabel = isToggling ? (isEnabled ? "Stopping..." : "Starting...") : isEnabled ? "Stop Auto Mode" : "Start Auto Mode";
  const pauseButtonLabel = isPausePending ? (isPaused ? "Resuming..." : "Pausing...") : isPaused ? "Resume Auto Mode" : "Pause Auto Mode";

  const activeAutoOrders = useMemo(
    () => orders.filter((order) => order.approved_by === "AUTO_MODE" && isActiveOrder(order)),
    [orders],
  );
  const sharedActiveEntryOrders = useMemo(
    () => orders.filter((order) => isActiveOrder(order)),
    [orders],
  );
  const remainingEntrySlots = Math.max(SHARED_ENTRY_SLOT_CAP - sharedActiveEntryOrders.length, 0);
  const sharedActiveSymbols = useMemo(
    () =>
      Array.from(
        new Set(
          sharedActiveEntryOrders
            .map((order) => order.symbol?.trim())
            .filter((symbol): symbol is string => Boolean(symbol)),
        ),
      ).sort(),
    [sharedActiveEntryOrders],
  );
  const blockerSummaries = useMemo(() => summarizeBlockers(latestAutoModeScanDetail), [latestAutoModeScanDetail]);
  const latestFilteredResults = useMemo(
    () => latestAutoModeScanDetail?.results.filter((result) => result.outcome === "FILTERED_OUT").slice(0, 5) ?? [],
    [latestAutoModeScanDetail],
  );
  const diagnosticsConclusion = useMemo(
    () => buildDiagnosticsConclusion({ blockerSummaries, remainingEntrySlots }),
    [blockerSummaries, remainingEntrySlots],
  );
  const recentClosedAutoOrders = useMemo(
    () =>
      orders
        .filter((order) => order.approved_by === "AUTO_MODE" && isClosedTrade(order))
        .sort((left, right) => {
          const leftTime = new Date(tradeTerminalTimestamp(left) ?? 0).getTime();
          const rightTime = new Date(tradeTerminalTimestamp(right) ?? 0).getTime();
          return rightTime - leftTime;
        })
        .slice(0, 10),
    [orders],
  );
  const autoModeAudit = useMemo(
    () => auditEntries.filter((entry) => isAutoModeAudit(entry.details, entry.event_type)).slice(0, 20),
    [auditEntries],
  );

  return (
    <div className="page-grid">
      <section className="card hero-card">
        <div>
          <p className="eyebrow">Portfolio Automation</p>
          <h2>Auto Mode</h2>
          <p className="muted">
            Run the AQRR strategy on each closed 15-minute candle and place up to 3 managed entry setups when the
            regime, net-3R feasibility, and diversification filters all align.
          </p>
          <p className="muted">Pause stops new AQRR entries, while managed positions can still be protected and closed if the strategy invalidates them.</p>
        </div>
        <div className="balance-pill">
          <span>{statusLabel}</span>
          <strong>
            {sharedActiveEntryOrders.length} / {SHARED_ENTRY_SLOT_CAP} entry slots
          </strong>
        </div>
      </section>

      <section className="card">
        <div className="section-head">
          <div>
            <h2>Controls</h2>
            <p className="muted">Start, pause, resume, or fully stop AQRR automation. Only stop cancels pending entry orders.</p>
          </div>
        </div>
        <ReadStatusNotice
          status={autoModeReadStatus}
          unavailableMessage="Auto Mode runtime status could not be refreshed from the backend."
          staleMessage="Showing the last successful Auto Mode runtime snapshot because the latest backend refresh failed."
        />
        <div className="settings-grid">
          <div className="field">
            <span>Auto Mode</span>
            {isEnabled ? (
              <div className="actions">
                <button
                  className="primary-button"
                  type="button"
                  disabled={isPausePending}
                  onClick={() => {
                    if (isPausePending) {
                      return;
                    }
                    void setAutoModePaused(!isPaused);
                  }}
                >
                  {pauseButtonLabel}
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={isToggling}
                  onClick={() => {
                    if (isToggling) {
                      return;
                    }
                    void setAutoModeEnabled(false);
                  }}
                >
                  {toggleButtonLabel}
                </button>
              </div>
            ) : (
              <button
                className="primary-button"
                type="button"
                disabled={isToggling}
                onClick={() => {
                  if (isToggling) {
                    return;
                  }
                  void setAutoModeEnabled(true);
                }}
              >
                {toggleButtonLabel}
              </button>
            )}
          </div>
          <div className="field">
            <span>Signal Schedule</span>
            <strong>{autoMode?.signal_schedule === "15m_closed_candle" ? "Closed 15m candles" : "AQRR managed"}</strong>
            <p className="muted">AQRR no longer exposes a user-editable scan interval.</p>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Runtime Summary</h2>
            <p className="muted">Live stop-risk capacity and AQRR session protection derived from the current Binance account balance.</p>
          </div>
        </div>
        <ReadStatusNotice
          status={autoModeReadStatus}
          unavailableMessage="Auto Mode risk and session summary could not be refreshed from the backend."
          staleMessage="Showing the last successful Auto Mode summary because the latest backend refresh failed."
        />
        <div className="scan-summary-grid">
          <div className="summary-stat">
            <span className="label">Status</span>
            <strong>{statusLabel}</strong>
            <p className="muted">{isPaused ? "New cycles are paused until resume." : `Next cycle ${formatTimestamp(autoMode?.next_cycle_at)}`}</p>
          </div>
          <div className="summary-stat">
            <span className="label">Risk Budget</span>
            <strong>${formatMoney(autoMode?.portfolio_risk_budget_usdt)}</strong>
            <p className="muted">Remaining risk budget per slot ${formatMoney(autoMode?.per_slot_risk_budget_usdt)}</p>
          </div>
          <div className="summary-stat">
            <span className="label">Active Risk</span>
            <strong>${formatMoney(autoMode?.active_risk_usdt)}</strong>
            <p className="muted">
              {sharedActiveEntryOrders.length} / {SHARED_ENTRY_SLOT_CAP} shared entry slots in use, {remainingEntrySlots} remaining
            </p>
          </div>
          <div className="summary-stat">
            <span className="label">Last Cycle</span>
            <strong>{formatTimestamp(autoMode?.last_cycle_completed_at)}</strong>
            <p className="muted">Started {formatTimestamp(autoMode?.last_cycle_started_at)}</p>
          </div>
          <div className="summary-stat">
            <span className="label">Kill Switch</span>
            <strong>{autoMode?.kill_switch_active ? "Active" : "Clear"}</strong>
            <p className="muted">{autoMode?.kill_switch_reason ?? "No AQRR session lockout triggered."}</p>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Why No New Orders?</h2>
            <p className="muted">Latest AQRR cycle diagnostics from the newest scan plus the current shared slot state.</p>
          </div>
        </div>
        <ReadStatusNotice
          status={latestAutoModeScanDetailReadStatus}
          unavailableMessage="Latest Auto Mode scan diagnostics could not be refreshed from the backend."
          staleMessage="Showing the last successful Auto Mode diagnostics because the latest backend refresh failed."
        />
        {latestAutoModeScanDetail == null ? (
          latestAutoModeScanDetailReadStatus.error ? null : <div className="notice-card">Latest Auto Mode scan diagnostics are unavailable right now.</div>
        ) : (
          <>
            <div className="notice-card">
              <p className="muted">{diagnosticsConclusion}</p>
            </div>
            <div className="scan-summary-grid">
              <div className="summary-stat">
                <span className="label">Latest Cycle</span>
                <strong>#{latestAutoModeScanDetail.cycle.id}</strong>
                <p className="muted">Completed {formatTimestamp(latestAutoModeScanDetail.cycle.completed_at)}</p>
              </div>
              <div className="summary-stat">
                <span className="label">Scan Output</span>
                <strong>
                  {latestAutoModeScanDetail.cycle.candidates_found} candidates, {latestAutoModeScanDetail.cycle.signals_qualified} qualified
                </strong>
                <p className="muted">{latestAutoModeScanDetail.cycle.symbols_scanned} symbols scanned</p>
              </div>
              <div className="summary-stat">
                <span className="label">Shared Slots</span>
                <strong>
                  {sharedActiveEntryOrders.length} / {SHARED_ENTRY_SLOT_CAP} in use
                </strong>
                <p className="muted">{remainingEntrySlots} slots remaining</p>
              </div>
              <div className="summary-stat">
                <span className="label">Active Symbols</span>
                <strong>{sharedActiveSymbols.length > 0 ? sharedActiveSymbols.join(", ") : "None"}</strong>
                <p className="muted">Current shared entry symbols</p>
              </div>
            </div>

            <div className="section-head">
              <div className="section-head-copy">
                <h2>Top Blocker Counts</h2>
                <p className="muted">Grouped from `FILTERED_OUT` AQRR setups in the latest cycle.</p>
              </div>
            </div>
            <TableContainer>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Blocker</th>
                    <th>Count</th>
                  </tr>
                </thead>
                <tbody>
                  {blockerSummaries.map((summary) => (
                    <tr key={summary.key}>
                      <td className="cell-wrap-tight">{summary.label}</td>
                      <td>{summary.count}</td>
                    </tr>
                  ))}
                  {blockerSummaries.length === 0 ? (
                    <tr>
                      <td colSpan={2} className="empty-state">
                        No blocker reasons were recorded for the latest cycle.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </TableContainer>

            <div className="section-head">
              <div className="section-head-copy">
                <h2>Top Filtered Symbols</h2>
                <p className="muted">Highest-ranked filtered AQRR setups from the latest cycle.</p>
              </div>
            </div>
            <TableContainer>
              <table className="data-table expansive-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Direction</th>
                    <th>Final Score</th>
                    <th>Filter Reasons</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {latestFilteredResults.map((result) => (
                    <tr key={`${result.symbol}:${result.direction ?? "NONE"}:${result.final_score ?? "NA"}`}>
                      <td>{result.symbol}</td>
                      <td>{result.direction ?? "-"}</td>
                      <td>{result.final_score ?? "-"}</td>
                      <td className="cell-wrap-tight">{joinFilterReasons(result) || "-"}</td>
                      <td className="cell-wrap">{result.reason_text ?? "-"}</td>
                    </tr>
                  ))}
                  {latestFilteredResults.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="empty-state">
                        No filtered symbols were recorded for the latest cycle.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </TableContainer>
          </>
        )}
      </section>

      <section>
        <div className="section-head">
          <div>
            <h2>Active Auto Mode Orders</h2>
            <p className="muted">Only orders created with the AQRR Auto Mode workflow are managed automatically.</p>
            <p className="muted">Each AQRR setup creates one entry, one full-size take-profit, and one full-size stop-loss.</p>
          </div>
        </div>
        <ReadStatusNotice
          status={ordersReadStatus}
          unavailableMessage="Active Auto Mode orders could not be refreshed from the backend."
          staleMessage="Showing the last successful Auto Mode order snapshot because the latest backend refresh failed."
        />
        <OrdersTable
          orders={activeAutoOrders}
          hideEmptyState={Boolean(ordersReadStatus.error)}
          emptyMessage="No active Auto Mode orders right now."
        />
      </section>

      <section className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Recent Filled / Closed Auto Mode Trades</h2>
            <p className="muted">Triggered AQRR trades remain visible here after TP/SL exits, AQRR protective closes, or external closure detection.</p>
          </div>
          <div className="section-head-meta">
            <p className="muted">{recentClosedAutoOrders.length} recent trades</p>
          </div>
        </div>
        <ReadStatusNotice
          status={ordersReadStatus}
          unavailableMessage="Recent Auto Mode trade history could not be refreshed from the backend."
          staleMessage="Showing the last successful Auto Mode trade history because the latest backend refresh failed."
        />
        <TableContainer>
          <table className="data-table wide-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Direction</th>
                <th>Opened</th>
                <th>Final Status</th>
                <th>Closed</th>
                <th>Close Type</th>
              </tr>
            </thead>
            <tbody>
              {recentClosedAutoOrders.map((order) => (
                <tr key={order.id}>
                  <td>{order.symbol}</td>
                  <td>{order.direction}</td>
                  <td>{formatTimestamp(order.triggered_at)}</td>
                  <td className="cell-wrap-tight">{order.status}</td>
                  <td>{formatTimestamp(tradeTerminalTimestamp(order))}</td>
                  <td className="cell-wrap-tight">{order.close_type ?? "-"}</td>
                </tr>
              ))}
              {recentClosedAutoOrders.length === 0 && !ordersReadStatus.error ? (
                <tr>
                  <td colSpan={6} className="empty-state">
                    No triggered Auto Mode trades have closed yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </TableContainer>
      </section>

      <section className="card">
        <div className="section-head">
          <div className="section-head-copy">
            <h2>Recent Auto Mode Activity</h2>
          </div>
          <div className="section-head-meta">
            <p className="muted">{autoModeAudit.length} recent events</p>
          </div>
        </div>
        <ReadStatusNotice
          status={auditEntriesReadStatus}
          unavailableMessage="Recent Auto Mode activity could not be refreshed from the backend."
          staleMessage="Showing the last successful Auto Mode activity snapshot because the latest backend refresh failed."
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
              {autoModeAudit.map((entry) => (
                <tr key={entry.id}>
                  <td>{new Date(entry.timestamp).toLocaleString()}</td>
                  <td className="cell-wrap-tight">{entry.event_type}</td>
                  <td>{entry.symbol ?? "-"}</td>
                  <td className="cell-wrap">{entry.message ?? "-"}</td>
                </tr>
              ))}
              {autoModeAudit.length === 0 && !auditEntriesReadStatus.error ? (
                <tr>
                  <td colSpan={4} className="empty-state">
                    No Auto Mode activity yet.
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
