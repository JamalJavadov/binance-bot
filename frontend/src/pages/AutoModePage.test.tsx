import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { AutoModePage } from "./AutoModePage";
import { useAppStore } from "../store/appStore";
import type { AutoModeStatus, Order, ScanCycleDetail, ScanSymbolResult } from "../types/api";

const initialState = useAppStore.getState();

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

function buildAutoModeStatus(overrides: Partial<AutoModeStatus> = {}): AutoModeStatus {
  return {
    enabled: true,
    paused: false,
    running: false,
    signal_schedule: "15m_closed_candle",
    kill_switch_active: false,
    kill_switch_reason: null,
    active_order_count: 1,
    active_risk_usdt: "15.00",
    portfolio_risk_budget_usdt: "90.00",
    per_slot_risk_budget_usdt: "30.00",
    last_cycle_started_at: "2026-03-29T10:00:00Z",
    last_cycle_completed_at: "2026-03-29T10:04:00Z",
    next_cycle_at: "2026-03-29T10:15:00Z",
    ...overrides,
  };
}

function buildOrder(overrides: Partial<Order> = {}): Order {
  return {
    id: 1,
    signal_id: 1,
    symbol: "BTCUSDT",
    direction: "LONG",
    leverage: 5,
    margin_type: "ISOLATED",
    entry_price: "100.00",
    stop_loss: "95.00",
    take_profit: "115.00",
    quantity: "1.50",
    position_margin: "40.00",
    notional_value: "300.00",
    rr_ratio: "3.0",
    entry_order_id: "101",
    tp_order_id: "201",
    sl_order_id: "202",
    status: "ORDER_PLACED",
    placed_at: "2026-03-29T09:00:00Z",
    triggered_at: null,
    closed_at: null,
    cancelled_at: null,
    cancel_reason: null,
    expires_at: "2026-03-30T20:00:00Z",
    realized_pnl: null,
    close_price: null,
    close_type: null,
    risk_budget_usdt: "80.00",
    risk_usdt_at_stop: "12.34",
    risk_pct_of_wallet: "1.2345",
    approved_by: "AUTO_MODE",
    created_at: "2026-03-28T20:00:00Z",
    updated_at: "2026-03-28T20:00:00Z",
    ...overrides,
  };
}

function buildScanResult(overrides: Partial<ScanSymbolResult> = {}): ScanSymbolResult {
  return {
    symbol: "BTCUSDT",
    direction: "LONG",
    outcome: "FILTERED_OUT",
    confirmation_score: 55,
    final_score: 80,
    score_breakdown: {},
    reason_text: "BTCUSDT skipped because the live mark is too far from entry.",
    filter_reasons: ["entry_too_far_from_mark"],
    error_message: null,
    ...overrides,
  };
}

function buildScanDetail(overrides: Partial<ScanCycleDetail> = {}): ScanCycleDetail {
  return {
    cycle: {
      id: 388,
      started_at: "2026-04-05T07:23:14Z",
      completed_at: "2026-04-05T07:27:47Z",
      status: "COMPLETE",
      symbols_scanned: 300,
      candidates_found: 0,
      signals_qualified: 0,
      trigger_type: "AUTO_MODE",
      error_message: null,
      progress_pct: 100,
    },
    detail_available: true,
    results: [],
    workflow: [],
    ...overrides,
  };
}

describe("AutoModePage", () => {
  it("renders a start button when Auto Mode is disabled and toggles it on", () => {
    const setAutoModeEnabled = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({
      setAutoModeEnabled,
      autoMode: buildAutoModeStatus({
        enabled: false,
        paused: false,
        active_order_count: 0,
        active_risk_usdt: "0.00",
        last_cycle_started_at: null,
        last_cycle_completed_at: null,
        next_cycle_at: null,
      }),
      orders: [],
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getByRole("button", { name: "Start Auto Mode" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Start Auto Mode" }));

    expect(setAutoModeEnabled).toHaveBeenCalledWith(true);
  });

  it("renders enabled AQRR controls and removes interval editing", () => {
    const setAutoModeEnabled = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({
      setAutoModeEnabled,
      setAutoModePaused: vi.fn().mockResolvedValue(undefined),
      autoMode: buildAutoModeStatus(),
      orders: [],
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getByRole("heading", { name: "Auto Mode" })).toBeInTheDocument();
    expect(
      screen.getByText(
        /Run the AQRR strategy on each closed 15-minute candle and place up to 3 managed entry setups/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Closed 15m candles")).toBeInTheDocument();
    expect(screen.getByText("AQRR no longer exposes a user-editable scan interval.")).toBeInTheDocument();
    expect(screen.getByText("$90.00")).toBeInTheDocument();
    expect(screen.getByText("Remaining risk budget per slot $30.00")).toBeInTheDocument();
    expect(screen.getByText("Clear")).toBeInTheDocument();
    expect(screen.queryByLabelText("Scan Interval (minutes)")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save Interval" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop Auto Mode" })).toBeInTheDocument();
  });

  it("shows shared entry slot usage from global active orders", () => {
    useAppStore.setState({
      setAutoModeEnabled: vi.fn().mockResolvedValue(undefined),
      setAutoModePaused: vi.fn().mockResolvedValue(undefined),
      autoMode: buildAutoModeStatus(),
      orders: [
        { id: 1, approved_by: "AUTO_MODE", status: "ORDER_PLACED" },
        { id: 2, approved_by: "LEGACY_MODE", status: "IN_POSITION" },
        { id: 3, approved_by: "LEGACY_MODE", status: "CLOSED_WIN" },
      ] as never[],
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getByText("2 / 3 entry slots")).toBeInTheDocument();
    expect(screen.getByText("2 / 3 shared entry slots in use, 1 remaining")).toBeInTheDocument();
    expect(
      screen.getByText("Each AQRR setup creates one entry, one full-size take-profit, and one full-size stop-loss."),
    ).toBeInTheDocument();
  });

  it("shows recently closed triggered Auto Mode trades without including never-triggered cancellations", () => {
    useAppStore.setState({
      setAutoModeEnabled: vi.fn().mockResolvedValue(undefined),
      setAutoModePaused: vi.fn().mockResolvedValue(undefined),
      autoMode: buildAutoModeStatus(),
      orders: [
        buildOrder({ id: 1, symbol: "ACTIVEUSDT", status: "ORDER_PLACED" }),
        buildOrder({
          id: 2,
          symbol: "WINUSDT",
          status: "CLOSED_WIN",
          triggered_at: "2026-03-29T10:00:00Z",
          closed_at: "2026-03-29T12:00:00Z",
          close_type: "TP",
        }),
        buildOrder({
          id: 3,
          symbol: "STALEUSDT",
          status: "CANCELLED_BY_BOT",
          triggered_at: null,
          cancelled_at: "2026-03-29T11:00:00Z",
          close_type: null,
        }),
      ],
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getByRole("heading", { name: "Recent Filled / Closed Auto Mode Trades" })).toBeInTheDocument();
    expect(screen.getByText("WINUSDT")).toBeInTheDocument();
    expect(screen.getAllByText("TP").length).toBeGreaterThan(0);
    expect(screen.queryByText("STALEUSDT")).not.toBeInTheDocument();
  });

  it("renders pause state and resumes without stopping Auto Mode", () => {
    const setAutoModeEnabled = vi.fn().mockResolvedValue(undefined);
    const setAutoModePaused = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({
      setAutoModeEnabled,
      setAutoModePaused,
      autoMode: buildAutoModeStatus({
        paused: true,
        next_cycle_at: "2026-03-29T10:24:00Z",
      }),
      orders: [buildOrder({ approved_by: "AUTO_MODE", status: "ORDER_PLACED" })],
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getAllByText("Paused")).toHaveLength(2);
    expect(screen.getByText("New cycles are paused until resume.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume Auto Mode" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop Auto Mode" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Resume Auto Mode" }));

    expect(setAutoModePaused).toHaveBeenCalledWith(false);
    expect(setAutoModeEnabled).not.toHaveBeenCalled();
  });

  it("renders AQRR blocker counts and top filtered symbols for the latest cycle", () => {
    useAppStore.setState({
      setAutoModeEnabled: vi.fn().mockResolvedValue(undefined),
      setAutoModePaused: vi.fn().mockResolvedValue(undefined),
      autoMode: buildAutoModeStatus({
        last_cycle_started_at: "2026-04-05T07:23:14Z",
        last_cycle_completed_at: "2026-04-05T07:27:47Z",
        next_cycle_at: "2026-04-05T07:32:47Z",
      }),
      orders: [buildOrder({ symbol: "JTOUSDT", status: "IN_POSITION", triggered_at: "2026-04-05T06:23:12Z" })],
      latestAutoModeScanDetail: buildScanDetail({
        results: [
          buildScanResult({
            symbol: "REZUSDT",
            filter_reasons: ["entry_too_far_from_mark"],
            final_score: 80,
            reason_text: "REZUSDT skipped because live mark is 6.35% away from entry.",
          }),
          buildScanResult({
            symbol: "BOMEUSDT",
            direction: "SHORT",
            filter_reasons: ["entry_too_far_from_mark"],
            final_score: 70,
            reason_text: "BOMEUSDT skipped because live mark is 5.58% away from entry.",
          }),
          buildScanResult({
            symbol: "ETCUSDT",
            direction: "SHORT",
            filter_reasons: ["stop_loss_crossed"],
            final_score: 100,
            reason_text: "ETCUSDT order could not be placed because the stop-loss would immediately trigger on Binance.",
          }),
          buildScanResult({
            symbol: "SPACEUSDT",
            direction: "SHORT",
            filter_reasons: ["correlation_conflict"],
            final_score: 80,
            reason_text: "SPACEUSDT conflicted with an existing correlated selection.",
          }),
          buildScanResult({
            symbol: "RUNEUSDT",
            direction: "SHORT",
            filter_reasons: ["spread_relative_above_threshold"],
            final_score: 60,
            reason_text: "RUNEUSDT spread expanded far above its rolling AQRR baseline.",
          }),
        ],
      }),
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getByRole("heading", { name: "Why No New Orders?" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "The latest AQRR cycle rejected setups because they failed execution quality, regime clarity, net-3R feasibility, or diversification controls.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("1 / 3 in use")).toBeInTheDocument();
    expect(screen.getByText("2 slots remaining")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Top Blocker Counts" })).toBeInTheDocument();
    expect(screen.getAllByText("Entry too far from mark").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Correlation conflict").length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "Top Filtered Symbols" })).toBeInTheDocument();
    expect(screen.getByText("ETCUSDT")).toBeInTheDocument();
    expect(screen.getByText("REZUSDT")).toBeInTheDocument();
    expect(screen.getByText("SPACEUSDT")).toBeInTheDocument();
    expect(screen.getAllByText("Stop-loss already crossed").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Relative spread above AQRR cap").length).toBeGreaterThan(0);
  });

  it("surfaces shared slot exhaustion ahead of other diagnostics", () => {
    useAppStore.setState({
      setAutoModeEnabled: vi.fn().mockResolvedValue(undefined),
      setAutoModePaused: vi.fn().mockResolvedValue(undefined),
      autoMode: buildAutoModeStatus(),
      orders: [
        buildOrder({ id: 1, symbol: "BTCUSDT", status: "ORDER_PLACED" }),
        buildOrder({ id: 2, symbol: "ETHUSDT", status: "ORDER_PLACED" }),
        buildOrder({ id: 3, symbol: "SOLUSDT", status: "IN_POSITION", triggered_at: "2026-04-05T06:23:12Z" }),
      ],
      latestAutoModeScanDetail: buildScanDetail({
        results: [
          buildScanResult({
            symbol: "XRPUSDT",
            filter_reasons: ["slot_limit_reached"],
            reason_text: "XRPUSDT ranked below the 3-slot AQRR capacity.",
          }),
        ],
      }),
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(
      screen.getByText(
        "New orders are currently capped by shared slot usage, so the latest cycle cannot open another setup until a slot frees up.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("3 / 3 shared entry slots in use, 0 remaining")).toBeInTheDocument();
    expect(screen.getAllByText("Selection slot limit reached").length).toBeGreaterThan(0);
  });

  it("shows the kill switch state in the runtime summary", () => {
    useAppStore.setState({
      setAutoModeEnabled: vi.fn().mockResolvedValue(undefined),
      setAutoModePaused: vi.fn().mockResolvedValue(undefined),
      autoMode: buildAutoModeStatus({
        kill_switch_active: true,
        kill_switch_reason: "2 consecutive full stop losses reached.",
      }),
      orders: [],
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("2 consecutive full stop losses reached.")).toBeInTheDocument();
  });

  it("shows a neutral diagnostics state when the latest scan detail is unavailable", () => {
    useAppStore.setState({
      setAutoModeEnabled: vi.fn().mockResolvedValue(undefined),
      setAutoModePaused: vi.fn().mockResolvedValue(undefined),
      autoMode: buildAutoModeStatus({
        active_order_count: 0,
        active_risk_usdt: "0.00",
        last_cycle_started_at: null,
        last_cycle_completed_at: null,
        next_cycle_at: null,
      }),
      orders: [],
      latestAutoModeScanDetail: null,
      auditEntries: [],
    });

    render(
      <StrictMode>
        <AutoModePage />
      </StrictMode>,
    );

    expect(screen.getByText("Latest Auto Mode scan diagnostics are unavailable right now.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Top Filtered Symbols" })).not.toBeInTheDocument();
  });
});
