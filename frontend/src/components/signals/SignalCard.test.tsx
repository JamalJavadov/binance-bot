import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SignalCard } from "./SignalCard";
import { useAppStore } from "../../store/appStore";
import type { Signal } from "../../types/api";

const initialState = useAppStore.getState();

function buildSignal(status: Signal["status"], preview: Record<string, unknown>): Signal {
  return {
    id: 1,
    scan_cycle_id: 1,
    symbol: "BCHUSDT",
    direction: "SHORT",
    timeframe: "4h",
    entry_price: "475.24",
    stop_loss: "481.44",
    take_profit: "456.63",
    rr_ratio: "3.0",
    confirmation_score: 72,
    final_score: 88,
    score_breakdown: { trend: 72 },
    reason_text: "Qualified setup",
    swing_origin: null,
    swing_terminus: null,
    fib_0786_level: null,
    current_price_at_signal: "482.95",
    expires_at: "2026-03-30T20:00:00Z",
    status,
    extra_context: { order_preview: preview },
    created_at: "2026-03-28T20:00:00Z",
    updated_at: "2026-03-28T20:00:00Z",
  };
}

describe("SignalCard", () => {
  it("renders auto-resized live margin previews", () => {
    useAppStore.setState({ pendingActions: {} });

    render(
      <SignalCard
        signal={buildSignal("QUALIFIED", {
          status: "resized_to_budget",
          can_place: true,
          auto_resized: true,
          requested_quantity: "2.157",
          final_quantity: "0.309",
          max_affordable_quantity: "0.309",
          mark_price_used: "482.95",
          entry_notional: "146.6486965",
          required_initial_margin: "14.923155",
          estimated_entry_fee: "0.08953893",
          available_balance: "16.72160381",
          reserve_balance: "1.672160381",
          usable_balance: "15.049443429",
          risk_budget_usdt: "13.377283048",
          risk_usdt_at_stop: "1.9162635",
          recommended_leverage: 10,
          reason: null,
        })}
        onApprove={() => {}}
        onDismiss={() => {}}
      />,
    );

    expect(screen.getByText("Will auto-resize")).toBeInTheDocument();
    expect(screen.getByText("Will place 0.309 instead of 2.157 to fit live margin.")).toBeInTheDocument();
    expect(screen.getByText("$14.923155")).toBeInTheDocument();
    expect(screen.getByText("Mark at Signal")).toBeInTheDocument();
    expect(screen.getByText("Sizing Mark")).toBeInTheDocument();
    expect(screen.getAllByText("482.95")).toHaveLength(2);
  });

  it("renders not-affordable previews with the backend reason", () => {
    useAppStore.setState({ pendingActions: {} });

    render(
      <SignalCard
        signal={buildSignal("QUALIFIED", {
          status: "too_small_for_exchange",
          can_place: false,
          auto_resized: false,
          requested_quantity: "0.129",
          final_quantity: "0.018",
          max_affordable_quantity: "0.018",
          mark_price_used: "482.95",
          entry_notional: "8.554293",
          required_initial_margin: "0.86931",
          estimated_entry_fee: "0.005216",
          available_balance: "1",
          reserve_balance: "0.1",
          usable_balance: "0.9",
          risk_budget_usdt: "0.8",
          risk_usdt_at_stop: "0.111627",
          recommended_leverage: 10,
          reason: "only $8.55 entry notional fits within live margin, below Binance minimum notional $20.00.",
        })}
        onApprove={() => {}}
        onDismiss={() => {}}
      />,
    );

    expect(screen.getByText("Too small for Binance")).toBeInTheDocument();
    expect(
      screen.getByText("only $8.55 entry notional fits within live margin, below Binance minimum notional $20.00."),
    ).toBeInTheDocument();
  });
});
