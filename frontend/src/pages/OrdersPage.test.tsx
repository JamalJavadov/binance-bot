import { StrictMode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { OrdersPage } from "./OrdersPage";
import { useAppStore } from "../store/appStore";
import type { Order } from "../types/api";

const initialState = useAppStore.getState();

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

function buildOrder(overrides: Partial<Order> = {}): Order {
  return {
    id: 101,
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

describe("OrdersPage", () => {
  it("filters by canonical order ID and keeps lifecycle result columns visible", () => {
    useAppStore.setState({
      orders: [
        buildOrder({ id: 101, symbol: "BTCUSDT" }),
        buildOrder({ id: 202, symbol: "ETHUSDT", realized_pnl: "-5.00", status: "CLOSED_LOSS", close_type: "SL" }),
      ],
    });

    render(
      <StrictMode>
        <OrdersPage />
      </StrictMode>,
    );

    expect(screen.getByRole("columnheader", { name: "Order ID" })).toBeInTheDocument();
    expect(
      screen.getByText((_, element) => element?.tagName === "P" && element.textContent === "2 shown • 2 closed trades"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search by Order ID or Symbol"), { target: { value: "202" } });

    expect(screen.getByText("ETHUSDT")).toBeInTheDocument();
    expect(screen.queryByText("BTCUSDT")).not.toBeInTheDocument();
    expect(screen.getByText("LOSS")).toBeInTheDocument();
    expect(screen.getByText("-$5.00")).toBeInTheDocument();
  });
});
