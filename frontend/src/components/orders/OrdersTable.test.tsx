import { StrictMode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import { OrdersTable } from "./OrdersTable";
import { useAppStore } from "../../store/appStore";
import type { Order } from "../../types/api";

const initialState = useAppStore.getState();

afterEach(() => {
  cleanup();
  useAppStore.setState(initialState, true);
});

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
    placed_at: null,
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

describe("OrdersTable", () => {
  it("renders profit targets for long orders alongside persisted risk and lifecycle metadata", () => {
    render(
      <StrictMode>
        <OrdersTable orders={[buildOrder()]} />
      </StrictMode>,
    );

    expect(
      screen.getByText("Each row is one setup for one coin. A live setup can create up to 4 Binance orders: entry, TP1, TP2, and stop loss."),
    ).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Recommended Profit" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Max Profit" })).toBeInTheDocument();
    expect(screen.getByText("$18.00")).toBeInTheDocument();
    expect(screen.getByText("$22.50")).toBeInTheDocument();
    expect(screen.getByText("$12.34")).toBeInTheDocument();
    expect(screen.getByText("1.23%")).toBeInTheDocument();
    expect(screen.getByText("AUTO_MODE")).toBeInTheDocument();
    expect(screen.getByText("3 / 3")).toBeInTheDocument();
  });

  it("renders partial TP orders with dual targets, weighted profit, and four-leg tracking", () => {
    render(
      <StrictMode>
        <OrdersTable
          orders={[
            buildOrder({
              quantity: "0.099",
              partial_tp_enabled: true,
              take_profit: "114.70",
              take_profit_1: "107.40",
              take_profit_2: "114.70",
              tp_quantity_1: "0.049",
              tp_quantity_2: "0.050",
              tp_order_1_id: "301",
              tp_order_2_id: "302",
              tp_order_id: "302",
              sl_order_id: "303",
            }),
          ]}
        />
      </StrictMode>,
    );

    expect(screen.getByText("107.40 / 114.70")).toBeInTheDocument();
    expect(screen.getByText("4 / 4")).toBeInTheDocument();
    expect(screen.getByText("$0.88")).toBeInTheDocument();
    expect(screen.getByText("$1.10")).toBeInTheDocument();
  });

  it("renders profit targets correctly for short orders", () => {
    render(
      <StrictMode>
        <OrdersTable
          orders={[
            buildOrder({
              id: 7,
              symbol: "ETHUSDT",
              direction: "SHORT",
              entry_price: "200.00",
              take_profit: "180.00",
              stop_loss: "210.00",
              quantity: "2.00",
            }),
          ]}
        />
      </StrictMode>,
    );

    const row = screen.getByText("ETHUSDT").closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("$32.00")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("$40.00")).toBeInTheDocument();
  });

  it("renders lifecycle columns with canonical order tracking details", () => {
    render(
      <StrictMode>
        <OrdersTable
          orders={[
            buildOrder({
              id: 42,
              status: "CLOSED_WIN",
              triggered_at: "2026-03-29T09:30:00Z",
              closed_at: "2026-03-29T11:00:00Z",
              close_type: "TP",
              realized_pnl: "24.50",
            }),
          ]}
          showLifecycleColumns
        />
      </StrictMode>,
    );

    const row = screen.getByText("42").closest("tr");
    expect(row).not.toBeNull();

    expect(screen.getByRole("columnheader", { name: "Order ID" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Recommended Profit" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Max Profit" })).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("42")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("$18.00")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("$22.50")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("TP")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("WIN")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("+$24.50")).toBeInTheDocument();
  });
});
