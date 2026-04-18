"""
app.validation.simulator

Minimal offline execution and fill simulator.
Given a validated AQRR setup candidate and a slice of historical future candles,
this evaluates deterministic logic to resolve whether the setup triggered
and if it hit stop-loss or take-profit.

Design principles:
  - Offline-first: stateless functions using pure generic inputs.
  - Conservative intra-bar fills: if a single OHLC candle touches both SL and TP,
    it is deterministically recorded as a LOSS to prevent false backtest optimism.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

from app.models.enums import SignalDirection
from app.services.strategy.types import Candle


class SimOutcome(Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    EXPIRED_NO_FILL = "EXPIRED_NO_FILL"
    UNRESOLVED = "UNRESOLVED"  # Ran out of historical future data before hitting SL/TP


@dataclass(frozen=True)
class SimResult:
    """Deterministic outcome record for a simulated execution."""
    outcome: SimOutcome
    fill_time_ms: int | None
    exit_time_ms: int | None


def simulate_execution(
    *,
    direction: SignalDirection,
    entry_style: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    expiry_bars: int,
    future_candles: list[Candle],
) -> SimResult:
    """
    Simulate the lifecycle of a single offline candidate via subsequent candles.

    Args:
        direction: LONG or SHORT
        entry_style: LIMIT_GTD or STOP_ENTRY
        entry_price: Trigger price for the order
        stop_loss: Stop-loss price
        take_profit: Take-profit price
        expiry_bars: Number of candles (inclusive) the entry order spans before cancelling
        future_candles: The slice of historical candles starting exactly *after* the
                        candle that generated the candidate.

    Returns:
        SimResult containing deterministic outcome, and timing metadata if applicable.
    """
    fill_time_ms: int | None = None
    fill_candle_idx: int | None = None

    # 1. Entry Matching Lifecycle
    for i, candle in enumerate(future_candles):
        if i >= expiry_bars:
            break

        filled = False
        if entry_style == "LIMIT_GTD":
            if direction == SignalDirection.LONG and candle.low <= entry_price:
                filled = True
            elif direction == SignalDirection.SHORT and candle.high >= entry_price:
                filled = True
        elif entry_style == "STOP_ENTRY":
            if direction == SignalDirection.LONG and candle.high >= entry_price:
                filled = True
            elif direction == SignalDirection.SHORT and candle.low <= entry_price:
                filled = True

        if filled:
            fill_time_ms = candle.open_time
            fill_candle_idx = i
            break

    # If the window expires without the entry triggering
    if fill_time_ms is None or fill_candle_idx is None:
        return SimResult(outcome=SimOutcome.EXPIRED_NO_FILL, fill_time_ms=None, exit_time_ms=None)

    # 2. Exit Resolution Lifecycle
    # Evaluation starts immediately on the fill candle.
    for i in range(fill_candle_idx, len(future_candles)):
        candle = future_candles[i]
        
        hit_sl = False
        hit_tp = False

        if direction == SignalDirection.LONG:
            # In a LIVE environment involving real spread/slippage, prices are often worse.
            # Offline simulator bounds ensure we test the maximum extremes for intersection.
            hit_sl = candle.low <= stop_loss
            hit_tp = candle.high >= take_profit
        elif direction == SignalDirection.SHORT:
            hit_sl = candle.high >= stop_loss
            hit_tp = candle.low <= take_profit

        # Conservative Principle Rule
        # If the same 15-minute candle exceeds BOTH the SL and TP bands,
        # we strictly assume the SL was hit first to avoid backtest overfitting.
        if hit_sl and hit_tp:
            return SimResult(
                outcome=SimOutcome.LOSS,
                fill_time_ms=fill_time_ms,
                exit_time_ms=candle.open_time
            )

        if hit_sl:
            return SimResult(
                outcome=SimOutcome.LOSS,
                fill_time_ms=fill_time_ms,
                exit_time_ms=candle.open_time
            )

        if hit_tp:
            return SimResult(
                outcome=SimOutcome.WIN,
                fill_time_ms=fill_time_ms,
                exit_time_ms=candle.open_time
            )

    # Reached the end of the data slice without touching either exit band
    return SimResult(
        outcome=SimOutcome.UNRESOLVED,
        fill_time_ms=fill_time_ms,
        exit_time_ms=None
    )
