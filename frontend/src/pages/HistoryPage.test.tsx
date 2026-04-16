import { StrictMode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { HistoryPage } from "./HistoryPage";
import { useAppStore } from "../store/appStore";
import type { Order } from "../types/api";

const initialState = useAppStore.getState();

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

function buildOrder(overrides: Partial<Order> = {}): Order {
  return {
    id: 12,
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
    status: "CLOSED_WIN",
    placed_at: "2026-03-29T09:00:00Z",
    triggered_at: "2026-03-29T09:30:00Z",
    closed_at: "2026-03-29T11:00:00Z",
    cancelled_at: null,
    cancel_reason: null,
    expires_at: "2026-03-30T20:00:00Z",
    realized_pnl: "24.50",
    close_price: "115.00",
    close_type: "TP",
    risk_budget_usdt: "80.00",
    risk_usdt_at_stop: "12.34",
    risk_pct_of_wallet: "1.2345",
    approved_by: "AUTO_MODE",
    created_at: "2026-03-28T20:00:00Z",
    updated_at: "2026-03-29T11:00:00Z",
    ...overrides,
  };
}

describe("HistoryPage", () => {
  it("renders trade journal summary above scan and audit history", () => {
    useAppStore.setState({
      orders: [
        buildOrder(),
        buildOrder({
          id: 13,
          symbol: "ETHUSDT",
          status: "CLOSED_LOSS",
          closed_at: "2026-03-29T12:00:00Z",
          realized_pnl: "-10.00",
          close_type: "SL",
        }),
      ],
      scanHistory: [
        {
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
        },
      ],
      auditEntries: [
        {
          id: 5,
          timestamp: "2026-03-29T12:05:00Z",
          event_type: "ORDER_CLOSED",
          level: "INFO",
          symbol: "BTCUSDT",
          message: "Order 12 closed at TP",
          details: {},
        },
      ],
    });

    render(
      <StrictMode>
        <HistoryPage />
      </StrictMode>,
    );

    expect(screen.getByRole("heading", { name: "Closed Positions" })).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) => element?.tagName === "P" && element.textContent === "2 closed positions • 1 wins • 1 losses • +$14.50",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Direction" })).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("WIN")).toBeInTheDocument();
    expect(screen.getByText("TP")).toBeInTheDocument();
    expect(screen.getByText("+$24.50")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Scan History" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Audit Log" })).toBeInTheDocument();
    expect(screen.getByText("Order 12 closed at TP")).toBeInTheDocument();
  });
});
