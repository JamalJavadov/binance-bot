import { StrictMode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { SignalsPage } from "./SignalsPage";
import { useAppStore } from "../store/appStore";
import type { RecommendedSignal, ScanCycle, Signal } from "../types/api";

const initialState = useAppStore.getState();

const completedScan: ScanCycle = {
  id: 7,
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

const olderCompletedScan: ScanCycle = {
  id: 6,
  started_at: "2026-03-27T20:00:00Z",
  completed_at: "2026-03-27T20:18:00Z",
  status: "COMPLETE",
  symbols_scanned: 50,
  candidates_found: 2,
  signals_qualified: 1,
  trigger_type: "AUTO_MODE",
  error_message: null,
  progress_pct: 100,
};

const runningScan: ScanCycle = {
  id: 8,
  started_at: "2026-03-28T21:00:00Z",
  completed_at: null,
  status: "RUNNING",
  symbols_scanned: 16,
  candidates_found: 1,
  signals_qualified: 0,
  trigger_type: "AUTO_MODE",
  error_message: null,
  progress_pct: 32,
};

function buildSignal(id: number, scanCycleId: number, status: Signal["status"]): Signal {
  return {
    id,
    scan_cycle_id: scanCycleId,
    scan_trigger_type: scanCycleId === completedScan.id ? completedScan.trigger_type : olderCompletedScan.trigger_type,
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
  };
}

function buildRecommendedSignal(id: number, symbol: string, canOpenNow = true): RecommendedSignal {
  return {
    rank: id,
    signal: {
      ...buildSignal(id, completedScan.id, "QUALIFIED"),
      symbol,
    },
    live_readiness: {
      mark_price: "100.00",
      order_preview: {
        status: canOpenNow ? "affordable" : "too_small_for_exchange",
        can_place: canOpenNow,
        auto_resized: false,
        requested_quantity: "1.0",
        final_quantity: "1.0",
        max_affordable_quantity: "1.0",
        mark_price_used: "100.00",
        entry_notional: "100.00",
        required_initial_margin: "10.00",
        estimated_entry_fee: "0.10",
        available_balance: "1000.00",
        reserve_balance: "100.00",
        usable_balance: "900.00",
        risk_budget_usdt: "800.00",
        risk_usdt_at_stop: "10.00",
        recommended_leverage: 10,
        reason: canOpenNow ? null : "below Binance minimum notional",
      },
      can_open_now: canOpenNow,
      failure_reason: canOpenNow ? null : "below Binance minimum notional",
    },
  };
}

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

describe("SignalsPage", () => {
  it("shows only signals from the latest completed scan", () => {
    useAppStore.setState({
      scanHistory: [runningScan, completedScan, olderCompletedScan],
      recommendedSignals: [buildRecommendedSignal(1, "BESTUSDT")],
      refreshRecommendedSignals: async () => undefined,
      signals: [
        buildSignal(1, completedScan.id, "QUALIFIED"),
        buildSignal(2, completedScan.id, "CANDIDATE"),
        buildSignal(3, olderCompletedScan.id, "QUALIFIED"),
        buildSignal(4, runningScan.id, "QUALIFIED"),
      ],
    });

    render(
      <StrictMode>
        <SignalsPage />
      </StrictMode>,
    );

    expect(screen.getByText("2 from the latest completed scan")).toBeInTheDocument();
    expect(screen.getByText("Latest completed scan: AUTO_MODE • AQRR Binance USD-M Strategy")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Top 3 Auto Mode Candidates" })).toBeInTheDocument();
    expect(screen.getByText("BESTUSDT")).toBeInTheDocument();
    expect(screen.getByText("BTCUSDT-1")).toBeInTheDocument();
    expect(screen.getByText("BTCUSDT-2")).toBeInTheDocument();
    expect(screen.queryByText("BTCUSDT-3")).not.toBeInTheDocument();
    expect(screen.queryByText("BTCUSDT-4")).not.toBeInTheDocument();
    expect(screen.getByText("Signals are read-only here. Auto Mode decides which qualified setup to place live.")).toBeInTheDocument();
  });

  it("shows an empty state when there is no completed scan yet", () => {
    useAppStore.setState({
      scanHistory: [runningScan],
      recommendedSignals: [],
      refreshRecommendedSignals: async () => undefined,
      signals: [buildSignal(1, runningScan.id, "QUALIFIED")],
    });

    render(
      <StrictMode>
        <SignalsPage />
      </StrictMode>,
    );

    expect(screen.getAllByText("No completed scan available yet.")).not.toHaveLength(0);
    expect(screen.queryByText("BTCUSDT-1")).not.toBeInTheDocument();
  });

  it("shows a backend error notice instead of an empty state when signals are unavailable", () => {
    useAppStore.setState((state) => ({
      scanHistory: [],
      recommendedSignals: [],
      refreshRecommendedSignals: async () => undefined,
      signals: [],
      readStates: {
        ...state.readStates,
        scanOverview: {
          loaded: false,
          stale: false,
          error: "Backend unavailable",
        },
        recommendedSignals: {
          loaded: false,
          stale: false,
          error: "Backend unavailable",
        },
        signals: {
          loaded: false,
          stale: false,
          error: "Backend unavailable",
        },
      },
    }));

    render(
      <StrictMode>
        <SignalsPage />
      </StrictMode>,
    );

    expect(screen.getByText("Signals could not be refreshed from the backend.")).toBeInTheDocument();
    expect(screen.getAllByText("Backend unavailable").length).toBeGreaterThan(0);
    expect(screen.queryByText("No completed scan available yet.")).not.toBeInTheDocument();
  });
});
