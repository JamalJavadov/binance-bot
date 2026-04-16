import type { Order, OrderStatus } from "../types/api";

export const ACTIVE_ORDER_STATUSES: ReadonlySet<OrderStatus> = new Set(["SUBMITTING", "ORDER_PLACED", "IN_POSITION"]);

export function isActiveOrder(order: Order): boolean {
  return ACTIVE_ORDER_STATUSES.has(order.status);
}

function orderProfitDistance(
  order: Pick<Order, "direction" | "entry_price">,
  targetPrice: string | number | null | undefined,
): number {
  const entryPrice = Number(order.entry_price);
  const takeProfit = Number(targetPrice ?? 0);

  if (order.direction === "LONG") {
    return takeProfit - entryPrice;
  }

  return entryPrice - takeProfit;
}

function partialTargetQuantity(order: Pick<Order, "quantity" | "tp_quantity_1" | "tp_quantity_2">, role: 1 | 2): number {
  const totalQuantity = Number(order.quantity);
  if (role === 1) {
    return Number(order.tp_quantity_1 ?? 0) || totalQuantity / 2;
  }
  const configuredQuantity = Number(order.tp_quantity_2 ?? 0);
  if (configuredQuantity > 0) {
    return configuredQuantity;
  }
  return Math.max(totalQuantity - partialTargetQuantity(order, 1), 0);
}

export function getOrderMaxProfitUsdt(
  order: Pick<
    Order,
    | "direction"
    | "entry_price"
    | "take_profit"
    | "quantity"
    | "partial_tp_enabled"
    | "take_profit_1"
    | "take_profit_2"
    | "tp_quantity_1"
    | "tp_quantity_2"
  >,
): number {
  if (order.partial_tp_enabled && order.take_profit_1 && (order.take_profit_2 ?? order.take_profit)) {
    const tp1Quantity = partialTargetQuantity(order, 1);
    const tp2Quantity = partialTargetQuantity(order, 2);
    const tp1Profit = orderProfitDistance(order, order.take_profit_1) * tp1Quantity;
    const tp2Profit = orderProfitDistance(order, order.take_profit_2 ?? order.take_profit) * tp2Quantity;
    return Math.max(tp1Profit + tp2Profit, 0);
  }

  const quantity = Number(order.quantity);
  const grossProfit = orderProfitDistance(order, order.take_profit) * quantity;
  return Math.max(grossProfit, 0);
}

export function getOrderRecommendedProfitUsdt(
  order: Pick<
    Order,
    | "direction"
    | "entry_price"
    | "take_profit"
    | "quantity"
    | "partial_tp_enabled"
    | "take_profit_1"
    | "take_profit_2"
    | "tp_quantity_1"
    | "tp_quantity_2"
  >,
): number {
  return getOrderMaxProfitUsdt(order) * 0.8;
}

export function getOrderTakeProfitDisplay(order: Pick<Order, "partial_tp_enabled" | "take_profit" | "take_profit_1" | "take_profit_2">): string {
  if (!order.take_profit && !order.take_profit_1 && !order.take_profit_2) {
    return "";
  }
  if (!order.partial_tp_enabled || !order.take_profit_1) {
    return order.take_profit ?? "";
  }
  return `${order.take_profit_1} / ${order.take_profit_2 ?? order.take_profit}`;
}

export function tradeTerminalTimestamp(order: Order): string | null {
  return order.closed_at ?? order.cancelled_at ?? order.updated_at ?? null;
}

export function isClosedTrade(order: Order): boolean {
  return order.triggered_at != null && order.closed_at != null;
}

export function summarizeClosedTrades(orders: Order[]) {
  const closedTrades = orders.filter(isClosedTrade);
  let realizedTotal = 0;
  let winningTrades = 0;
  let losingTrades = 0;

  for (const order of closedTrades) {
    const realized = Number(order.realized_pnl ?? 0);
    realizedTotal += realized;
    if (realized > 0 || order.status === "CLOSED_WIN") {
      winningTrades += 1;
      continue;
    }
    if (realized < 0 || order.status === "CLOSED_LOSS") {
      losingTrades += 1;
    }
  }

  return {
    closedTrades,
    realizedTotal,
    winningTrades,
    losingTrades,
  };
}
