import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

import { BestOrdersSection } from "./BestOrdersSection";
import { useAppStore } from "../../store/appStore";
import type { RecommendedSignal } from "../../types/api";

const initialState = useAppStore.getState();

function buildRecommendedSignal(rank: number, symbol: string, canOpenNow: boolean): RecommendedSignal {
  return {
    rank,
    signal: {
      id: rank,
      scan_cycle_id: 7,
      scan_trigger_type: "AUTO_MODE",
      strategy_key: "aqrr_binance_usdm",
      strategy_label: "AQRR Binance USD-M Strategy",
      symbol,
      direction: "LONG",
      timeframe: "15m",
      entry_price: "100.00",
      stop_loss: "95.00",
      take_profit: "115.00",
      rr_ratio: "3.0",
      confirmation_score: 70,
      final_score: 90 - rank,
      score_breakdown: { trend: 90 - rank },
      reason_text: "Qualified setup",
      swing_origin: null,
      swing_terminus: null,
      fib_0786_level: null,
      current_price_at_signal: "100.00",
      expires_at: "2026-03-30T20:00:00Z",
      status: "QUALIFIED",
      extra_context: {},
      created_at: "2026-03-28T20:00:00Z",
      updated_at: "2026-03-28T20:00:00Z",
    },
    live_readiness: {
      mark_price: "99.00",
      order_preview: {
        status: canOpenNow ? "affordable" : "too_small_for_exchange",
        can_place: canOpenNow,
        auto_resized: false,
        requested_quantity: "1.0",
        final_quantity: "1.0",
        max_affordable_quantity: "1.0",
        mark_price_used: "99.00",
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
      failure_reason: canOpenNow ? null : "The entry level has already been crossed.",
    },
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useAppStore.setState(initialState, true);
});

describe("BestOrdersSection", () => {
  it("refreshes live recommendations on mount and every 15 seconds", () => {
    vi.useFakeTimers();
    const refreshRecommendedSignals = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({
      recommendedSignals: [],
      refreshRecommendedSignals,
    });

    render(<BestOrdersSection />);

    expect(refreshRecommendedSignals).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(refreshRecommendedSignals).toHaveBeenCalledTimes(2);
  });

  it("renders green and red recommendation cards with the correct action state", () => {
    const refreshRecommendedSignals = vi.fn().mockResolvedValue(undefined);
    useAppStore.setState({
      refreshRecommendedSignals,
      recommendedSignals: [
        buildRecommendedSignal(1, "GREENUSDT", true),
        buildRecommendedSignal(2, "REDUSDT", false),
      ],
      recommendationsScanId: 7,
      recommendationsScanTriggerType: "AUTO_MODE",
      recommendationsStrategyLabel: "AQRR Binance USD-M Strategy",
      recommendationsRefreshedAt: "2026-03-28T20:00:00Z",
    });

    render(<BestOrdersSection />);

    expect(screen.getByText("GREENUSDT")).toBeInTheDocument();
    expect(screen.getByText("REDUSDT")).toBeInTheDocument();
    expect(screen.getByText("Ready for Binance")).toBeInTheDocument();
    expect(screen.getByText("Blocked on Binance")).toBeInTheDocument();
    expect(screen.getByText("The entry level has already been crossed.")).toBeInTheDocument();
    expect(screen.getByText("Latest completed scan: AUTO_MODE • AQRR Binance USD-M Strategy")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open Order" })).not.toBeInTheDocument();
  });
});
