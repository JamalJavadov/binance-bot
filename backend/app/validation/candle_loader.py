"""
app.validation.candle_loader

Pure, stateless helpers for loading and transforming historical candle data.
Used by the offline backtest runner and walk-forward pipeline.

All functions accept and return plain `Candle` objects from app.services.strategy.types.
No DB, no gateway, no config — just data.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from app.services.strategy.types import Candle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BARS_PER_1H = 4    # 15m bars in one 1h candle
BARS_PER_4H = 16   # 15m bars in one 4h candle

# Minimum history depths required before evaluate_symbol is called
MIN_15M_BARS = 80
MIN_1H_BARS = 60
MIN_4H_BARS = 60


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def load_candles_from_csv(path: Path) -> list[Candle]:
    """
    Load OHLCV rows from a CSV file produced by historical_fetch.py.

    Expected columns (from Binance klines endpoint):
        open_time, open, high, low, close, volume, ...

    Returns a list of Candle objects sorted ascending by open_time.
    Malformed rows are skipped with a warning.
    """
    candles: list[Candle] = []
    if not path.exists():
        logger.warning("CSV not found: %s", path)
        return candles

    with open(path, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                candles.append(
                    Candle(
                        open_time=int(row["open_time"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                )
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed CSV row: %s", exc)

    candles.sort(key=lambda c: c.open_time)
    logger.debug("Loaded %d candles from %s", len(candles), path.name)
    return candles


# ---------------------------------------------------------------------------
# Timeframe aggregation
# ---------------------------------------------------------------------------

def _aggregate(candles_15m: list[Candle], chunk_size: int) -> list[Candle]:
    """
    Aggregate 15m candles into higher-timeframe OHLCV candles.
    Only complete chunks are emitted — no partial bars.
    """
    aggregated: list[Candle] = []
    total = len(candles_15m)
    # Walk in steps, stop before any incomplete tail chunk
    for i in range(0, total - chunk_size + 1, chunk_size):
        chunk = candles_15m[i : i + chunk_size]
        if len(chunk) < chunk_size:
            break
        aggregated.append(
            Candle(
                open_time=chunk[0].open_time,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
            )
        )
    return aggregated


def derive_1h_candles(candles_15m: list[Candle]) -> list[Candle]:
    """Aggregate 15m candles into synthetic 1h candles (4 bars each)."""
    return _aggregate(candles_15m, BARS_PER_1H)


def derive_4h_candles(candles_15m: list[Candle]) -> list[Candle]:
    """Aggregate 15m candles into synthetic 4h candles (16 bars each)."""
    return _aggregate(candles_15m, BARS_PER_4H)


# ---------------------------------------------------------------------------
# Window slicing
# ---------------------------------------------------------------------------

def candles_up_to(candles: list[Candle], *, end_time_ms: int, window: int) -> list[Candle]:
    """
    Return the most recent `window` candles whose open_time <= end_time_ms.

    Args:
        candles:     Full sorted candle list.
        end_time_ms: Inclusive upper bound (milliseconds).
        window:      Maximum number of candles to return.

    Returns:
        A slice of at most `window` candles, all at or before end_time_ms.
    """
    eligible = [c for c in candles if c.open_time <= end_time_ms]
    return eligible[-window:]


def candles_in_range(
    candles: list[Candle], *, start_time_ms: int, end_time_ms: int
) -> list[Candle]:
    """
    Return all candles whose open_time falls within [start_time_ms, end_time_ms].
    """
    return [c for c in candles if start_time_ms <= c.open_time <= end_time_ms]
