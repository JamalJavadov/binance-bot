import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import { DashboardPage } from "./DashboardPage";
import { useAppStore } from "../store/appStore";
import type { Order, Position, RecommendedSignal, ScanCycle, ScanCycleDetail, Signal } from "../types/api";

const initialState = useAppStore.getState();

const latestScan: ScanCycle = {
  id: 1,
  started_at: "2026-03-28T20:00:00Z",
  completed_at: "2026-03-28T20:20:00Z",
  status: "COMPLETE",
  symbols_scanned: 50,
  candidates_found: 4,
  signals_qualified: 3,
  trigger_type: "AUTO_MODE",
  error_message: null,
  progress_pct: 100,
};

const previousScan: ScanCycle = {
  id: 2,
  started_at: "2026-03-27T20:00:00Z",
  completed_at: "2026-03-27T20:18:00Z",
  status: "COMPLETE",
  symbols_scanned: 50,
  candidates_found: 3,
  signals_qualified: 2,
  trigger_type: "AUTO_MODE",
  error_message: null,
  progress_pct: 100,
};

const runningScan: ScanCycle = {
  id: 3,
  started_at: "2026-03-28T21:00:00Z",
  completed_at: null,
  status: "RUNNING",
  symbols_scanned: 22,
  candidates_found: 1,
  signals_qualified: 0,
  trigger_type: "AUTO_MODE",
  error_message: null,
  progress_pct: 44,
};

const buildSignal = (id: number, status: Signal["status"], scanCycleId = 1): Signal => ({
  id,
  scan_cycle_id: scanCycleId,
  scan_trigger_type: scanCycleId === latestScan.id ? latestScan.trigger_type : previousScan.trigger_type,
  strategy_key: "aqrr_binance_usdm",
  strategy_label: "AQRR Binance USD-M Strategy",
  symbol: `BTCUSDT-${id}`,
  direction: "LONG",
  timeframe: "15m",
  entry_price: "100.00",
  stop_loss: "95.00",
  take_profit: "115.00",
  rr_ratio: "3.0",
  confirmation_score: 60,
  final_score: 72,
  score_breakdown: { trend: 30, structure: 42 },
  reason_text: "Qualified setup",
  swing_origin: null,
  swing_terminus: null,
  fib_0786_level: null,
  current_price_at_signal: null,
  expires_at: "2026-03-30T20:00:00Z",
  status,
  extra_context: {},
  created_at: "2026-03-28T20:00:00Z",
  updated_at: "2026-03-28T20:00:00Z",
});

const buildRecommendedSignal = (
  id: number,
  overrides: Partial<RecommendedSignal> = {},
  signalOverrides: Partial<Signal> = {},
): RecommendedSignal => ({
  rank: id,
  live_readiness: {
    mark_price: "100.00",
    order_preview: {
      status: "affordable",
      can_place: true,
      auto_resized: false,
      requested_quantity: "2.0",
      final_quantity: "2.0",
      max_affordable_quantity: "2.0",
      mark_price_used: "100.00",
      entry_notional: "200.00",
      required_initial_margin: "20.00",
      estimated_entry_fee: "0.20",
      available_balance: "1000.00",
      reserve_balance: "100.00",
      usable_balance: "900.00",
      risk_budget_usdt: "800.00",
      risk_usdt_at_stop: "10.00",
      recommended_leverage: 10,
      reason: null,
    },
    can_open_now: true,
    failure_reason: null,
  },
  signal: {
    ...buildSignal(id, "QUALIFIED", latestScan.id),
    ...signalOverrides,
  },
  ...overrides,
});

const buildOrder = (id: number, status: Order["status"]): Order => ({
  id,
  signal_id: id,
  symbol: `ETHUSDT-${id}`,
  direction: "SHORT",
  leverage: 5,
  margin_type: "ISOLATED",
  entry_price: "200.00",
  stop_loss: "205.00",
  take_profit: "185.00",
  quantity: "1.50",
  position_margin: "40.00",
  notional_value: "300.00",
  rr_ratio: "3.0",
  entry_order_id: null,
  tp_order_id: null,
  sl_order_id: null,
  status,
  placed_at: null,
  triggered_at: null,
  closed_at: null,
  cancelled_at: null,
  cancel_reason: null,
  expires_at: "2026-03-30T20:00:00Z",
  realized_pnl: null,
  close_price: null,
  close_type: null,
  risk_budget_usdt: "100.00",
  risk_usdt_at_stop: "12.00",
  risk_pct_of_wallet: "1.20",
  approved_by: "AUTO_MODE",
  created_at: "2026-03-28T20:00:00Z",
  updated_at: "2026-03-28T20:00:00Z",
});

const buildPosition = (symbol: string, overrides: Partial<Position> = {}): Position => ({
  symbol,
  position_side: "BOTH",
  direction: "LONG",
  position_amount: 0.75,
  entry_price: 100,
  mark_price: 110,
  unrealized_pnl: 7.5,
  leverage: 7,
  source_kind: "EXTERNAL",
  linked_order_id: null,
  first_seen_at: "2026-03-30T10:00:00Z",
  last_seen_at: "2026-03-30T10:05:00Z",
  closed_at: null,
  ...overrides,
});

const detailedScan: ScanCycleDetail = {
  cycle: latestScan,
  detail_available: true,
  workflow: [
    {
      id: 2,
      timestamp: "2026-03-28T20:05:00Z",
      event_type: "SIGNAL_QUALIFIED",
      level: "INFO",
      symbol: "BTCUSDT",
      message: "BTCUSDT qualified",
    },
    {
      id: 1,
      timestamp: "2026-03-28T20:00:00Z",
      event_type: "SCAN_STARTED",
      level: "INFO",
      symbol: null,
      message: "Scan started",
    },
  ],
  results: [
    {
      symbol: "ADAUSDT",
      direction: "LONG",
      outcome: "FILTERED_OUT",
      confirmation_score: 58,
      final_score: 61,
      score_breakdown: { ema200: 30 },
      reason_text: "ema confluence",
      filter_reasons: ["duplicate_pending_order"],
      error_message: null,
    },
    {
      symbol: "BTCUSDT",
      direction: "LONG",
      outcome: "QUALIFIED",
      confirmation_score: 66,
      final_score: 85,
      score_breakdown: { ema200: 30, trendline: 20 },
      reason_text: "trendline + ema",
      filter_reasons: [],
      error_message: null,
    },
    {
      symbol: "XRPUSDT",
      direction: null,
      outcome: "NO_SETUP",
      confirmation_score: null,
      final_score: null,
      score_breakdown: {},
      reason_text: "mixed_structure",
      filter_reasons: [],
      error_message: null,
    },
  ],
};

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

describe("DashboardPage", () => {
  it("renders empty states without crashing", () => {
    useAppStore.setState({
      latestScan: null,
      scanHistory: [],
      selectedScanId: null,
      selectedScanDetail: null,
      recommendedSignals: [],
      refreshRecommendedSignals: vi.fn().mockResolvedValue(undefined),
      orders: [],
    });

    render(
      <StrictMode>
        <DashboardPage />
      </StrictMode>,
    );

    expect(screen.getByText("Latest auto-managed market scan")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Portfolio Monitor" })).toBeInTheDocument();
    expect(screen.getByText("No open Binance positions are being monitored right now.")).toBeInTheDocument();
    expect(screen.getByText("No scan selected yet.")).toBeInTheDocument();
    expect(screen.getByText("No completed scan available yet.")).toBeInTheDocument();
    expect(
      screen.getByText("Three active setups can mean 9 Binance orders in total, because each setup manages entry, TP, and SL."),
    ).toBeInTheDocument();
    expect(screen.getByText("No orders yet.")).toBeInTheDocument();
  });

  it("lets the user switch the selected scan from the dashboard selector", () => {
    const selectScanSpy = vi.fn(async (_scanId: number) => {});
    useAppStore.setState({
      latestScan,
      scanHistory: [latestScan, previousScan],
      selectedScanId: latestScan.id,
      selectedScanDetail: detailedScan,
      recommendedSignals: [],
      refreshRecommendedSignals: vi.fn().mockResolvedValue(undefined),
      selectScan: selectScanSpy,
    });

    render(
      <StrictMode>
        <DashboardPage />
      </StrictMode>,
    );

    fireEvent.change(screen.getByRole("combobox"), { target: { value: String(previousScan.id) } });

    expect(selectScanSpy).toHaveBeenCalledWith(previousScan.id);
  });

  it("renders workflow timeline, sorted scan results, live best orders, and active orders", () => {
    useAppStore.setState({
      latestScan,
      scanHistory: [latestScan, previousScan],
      selectedScanId: latestScan.id,
      selectedScanDetail: detailedScan,
      refreshRecommendedSignals: vi.fn().mockResolvedValue(undefined),
      recommendedSignals: [
        buildRecommendedSignal(1, {}, { symbol: "BTCUSDT-1" }),
        buildRecommendedSignal(2, {}, { symbol: "BTCUSDT-3" }),
        buildRecommendedSignal(3, {}, { symbol: "BTCUSDT-4" }),
      ],
      orders: [
        buildOrder(1, "ORDER_PLACED"),
        buildOrder(2, "IN_POSITION"),
        buildOrder(3, "CANCELLED_BY_USER"),
      ],
      portfolioSummary: {
        open_position_count: 2,
        winning_position_count: 1,
        losing_position_count: 1,
        total_unrealized_pnl: 5,
        last_synced_at: "2026-03-30T10:05:00Z",
      },
      positions: [
        buildPosition("BTCUSDT"),
        buildPosition("ETHUSDT", { direction: "SHORT", source_kind: "APP_LINKED", unrealized_pnl: -2.5 }),
      ],
    });

    render(
      <StrictMode>
        <DashboardPage />
      </StrictMode>,
    );

    expect(screen.getByText("Latest scan: COMPLETE (100%) • AUTO_MODE")).toBeInTheDocument();
    expect(screen.getByText("Selected source: AUTO_MODE • AQRR Binance USD-M Strategy")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Workflow Timeline" })).toBeInTheDocument();
    expect(screen.getByText("SCAN_STARTED")).toBeInTheDocument();
    expect(screen.getByText("SIGNAL_QUALIFIED")).toBeInTheDocument();

    const resultsSection = screen.getByRole("heading", { name: "Scanned Coins" }).closest("section");
    expect(resultsSection).not.toBeNull();
    const resultsRows = within(resultsSection as HTMLElement).getAllByRole("row");
    expect(resultsRows[1]).toHaveTextContent("BTCUSDT");
    expect(resultsRows[2]).toHaveTextContent("ADAUSDT");
    expect(resultsRows[2]).toHaveTextContent("duplicate_pending_order");
    expect(resultsRows[3]).toHaveTextContent("XRPUSDT");

    expect(screen.getByRole("heading", { name: "Top 3 Auto Mode Candidates" })).toBeInTheDocument();
    expect(screen.getByText("Auto Mode ranks these setups for the next cycle and places live orders on its own.")).toBeInTheDocument();
    expect(screen.getByText("BTCUSDT-1")).toBeInTheDocument();
    expect(screen.getByText("BTCUSDT-3")).toBeInTheDocument();
    expect(screen.getByText("BTCUSDT-4")).toBeInTheDocument();
    expect(screen.getAllByText("BTCUSDT").length).toBeGreaterThan(0);
    expect(screen.getByText("APP_LINKED")).toBeInTheDocument();
    expect(screen.getByText("+$5.00")).toBeInTheDocument();
    expect(screen.getByText("ETHUSDT-1")).toBeInTheDocument();
    expect(screen.getByText("ETHUSDT-2")).toBeInTheDocument();
    expect(screen.queryByText("ETHUSDT-3")).not.toBeInTheDocument();
  });

  it("keeps showing live recommendations while a newer scan is running", () => {
    useAppStore.setState({
      latestScan: runningScan,
      scanHistory: [runningScan, latestScan, previousScan],
      selectedScanId: runningScan.id,
      selectedScanDetail: {
        cycle: runningScan,
        detail_available: true,
        workflow: [],
        results: [],
      },
      refreshRecommendedSignals: vi.fn().mockResolvedValue(undefined),
      recommendedSignals: [
        buildRecommendedSignal(1, {}, { symbol: "BTCUSDT-1", scan_cycle_id: latestScan.id }),
      ],
    });

    render(
      <StrictMode>
        <DashboardPage />
      </StrictMode>,
    );

    expect(screen.getByText("BTCUSDT-1")).toBeInTheDocument();
  });

  it("shows the legacy scan notice without rendering detail tables", () => {
    useAppStore.setState({
      latestScan: previousScan,
      scanHistory: [previousScan],
      selectedScanId: previousScan.id,
      refreshRecommendedSignals: vi.fn().mockResolvedValue(undefined),
      selectedScanDetail: {
        cycle: previousScan,
        detail_available: false,
        workflow: [],
        results: [],
      },
    });

    render(
      <StrictMode>
        <DashboardPage />
      </StrictMode>,
    );

    expect(
      screen.getByText("Full workflow and per-coin detail is only available for scans created after this update."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Workflow Timeline" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Scanned Coins" })).not.toBeInTheDocument();
  });

  it("keeps realized bot trade totals separate from open-position P&L", () => {
    useAppStore.setState({
      latestScan,
      scanHistory: [latestScan],
      selectedScanId: latestScan.id,
      selectedScanDetail: detailedScan,
      refreshRecommendedSignals: vi.fn().mockResolvedValue(undefined),
      orders: [
        buildOrder(1, "ORDER_PLACED"),
        {
          ...buildOrder(2, "CLOSED_WIN"),
          triggered_at: "2026-03-29T10:00:00Z",
          closed_at: "2026-03-29T12:00:00Z",
          realized_pnl: "25.00",
          close_type: "TP",
        },
        {
          ...buildOrder(3, "CLOSED_LOSS"),
          triggered_at: "2026-03-29T11:00:00Z",
          closed_at: "2026-03-29T13:00:00Z",
          realized_pnl: "-5.00",
          close_type: "SL",
        },
      ],
      portfolioSummary: {
        open_position_count: 1,
        winning_position_count: 1,
        losing_position_count: 0,
        total_unrealized_pnl: 7.5,
        last_synced_at: "2026-03-30T10:05:00Z",
      },
      positions: [buildPosition("BTCUSDT")],
    });

    render(
      <StrictMode>
        <DashboardPage />
      </StrictMode>,
    );

    const openPnlCard = screen.getByText("Open P&L").closest(".summary-stat");
    const realizedPnlCard = screen.getByText("Realized P&L").closest(".summary-stat");

    expect(screen.getByRole("heading", { name: "Closed Bot Trades" })).toBeInTheDocument();
    expect(screen.getByText("Open P&L")).toBeInTheDocument();
    expect(openPnlCard).not.toBeNull();
    expect(within(openPnlCard as HTMLElement).getByText("+$7.50")).toBeInTheDocument();
    expect(screen.getByText("Realized P&L")).toBeInTheDocument();
    expect(realizedPnlCard).not.toBeNull();
    expect(within(realizedPnlCard as HTMLElement).getByText("+$20.00")).toBeInTheDocument();
    expect(screen.getAllByText("1")).not.toHaveLength(0);
  });
});
