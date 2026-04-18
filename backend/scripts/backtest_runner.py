#!/usr/bin/env python3
"""
backtest_runner.py

Offline AQRR backtest runner — single-symbol, single time-range.

Consumes pre-downloaded historical candle CSV files (produced by historical_fetch.py),
feeds the real evaluate_symbol() function from aqrr.py, and produces a structured
JSON artifact of candidate signals per-bar.

This runner is:
  - Fully offline (no DB, no gateway, no credentials)
  - Non-destructive (read-only from CSV files)
  - Not a full execution simulator (no fills, no PnL tracking in this phase)

Usage:
  python backend/scripts/backtest_runner.py \\
    --symbol BTCUSDT \\
    --data-dir data/historical \\
    --output data/backtest/BTCUSDT_run.json \\
    --start 1672531200000 \\
    --end 1680307200000
"""

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import argparse
import json
import logging
import time
from pathlib import Path

from app.models.enums import SignalDirection
from app.services.strategy.config import resolve_strategy_config
from app.validation.candle_loader import (
    MIN_15M_BARS, MIN_1H_BARS, MIN_4H_BARS,
    load_candles_from_csv,
    derive_1h_candles,
    derive_4h_candles,
    candles_up_to,
)
from app.validation.step_evaluator import run_step
from app.validation.simulator import simulate_execution

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BAR_MS = 15 * 60 * 1000  # 15 minutes in milliseconds
WINDOW_15M = MIN_15M_BARS + 20
WINDOW_1H = MIN_1H_BARS + 10
WINDOW_4H = MIN_4H_BARS + 10


def run_backtest(
    *,
    symbol: str,
    data_dir: Path,
    output_path: Path,
    start_ms: int,
    end_ms: int,
    step_bars: int = 4,
) -> None:
    """
    Run the offline backtest for one symbol over a time range.

    Args:
        symbol:      Trading pair (e.g. BTCUSDT).
        data_dir:    Directory containing {SYMBOL}_15m.csv files.
        output_path: Where to write the JSON result artifact.
        start_ms:    Start timestamp in milliseconds (inclusive).
        end_ms:      End timestamp in milliseconds (exclusive).
        step_bars:   Evaluation interval in 15m bars (default 4 = 1 hour).
    """
    config = resolve_strategy_config({})

    csv_path = data_dir / f"{symbol.upper()}_15m.csv"
    all_15m = load_candles_from_csv(csv_path)
    if not all_15m:
        logger.error("No candle data for %s. Run historical_fetch.py first.", symbol)
        return

    all_1h = derive_1h_candles(all_15m)
    all_4h = derive_4h_candles(all_15m)

    step_ms = step_bars * BAR_MS
    eval_times = list(range(start_ms, end_ms, step_ms))
    logger.info("Running backtest for %s: %d evaluation steps", symbol, len(eval_times))

    results = []
    run_start = time.time()

    for eval_time in eval_times:
        window_15m = candles_up_to(all_15m, end_time_ms=eval_time, window=WINDOW_15M)
        window_1h = candles_up_to(all_1h, end_time_ms=eval_time, window=WINDOW_1H)
        window_4h = candles_up_to(all_4h, end_time_ms=eval_time, window=WINDOW_4H)

        step = run_step(
            symbol=symbol,
            candles_15m=window_15m,
            candles_1h=window_1h,
            candles_4h=window_4h,
            eval_time_ms=eval_time,
            config=config,
        )
        
        if not step.skipped and step.candidates:
            future_candles = [c for c in all_15m if c.open_time > eval_time]
            for candidate in step.candidates:
                direction_enum = (
                    SignalDirection.LONG if candidate.direction == "LONG" else SignalDirection.SHORT
                )
                sim_res = simulate_execution(
                    direction=direction_enum,
                    entry_style=candidate.entry_style,
                    entry_price=candidate.entry_price,
                    stop_loss=candidate.stop_loss,
                    take_profit=candidate.take_profit,
                    expiry_bars=candidate.expiry_bars,
                    future_candles=future_candles
                )
                candidate.sim_outcome = sim_res.outcome.value
                candidate.sim_fill_time_ms = sim_res.fill_time_ms
                candidate.sim_exit_time_ms = sim_res.exit_time_ms
                
        results.append(step.to_dict())

    elapsed = time.time() - run_start
    candidates_found = sum(1 for r in results if r.get("candidates"))
    skipped = sum(1 for r in results if r.get("skipped"))

    artifact = {
        "symbol": symbol,
        "generated_at_ms": int(time.time() * 1000),
        "run_duration_seconds": round(elapsed, 2),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "total_steps": len(results),
        "steps_skipped": skipped,
        "steps_with_candidates": candidates_found,
        "results": results,
        "notes": [
            "Phase 2: no fills, no PnL, no position tracking.",
            "quote_volume, spread_bps, available_balance are fixed placeholders.",
            "Higher timeframe candles are derived from 15m aggregation.",
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=2)

    logger.info(
        "Backtest complete. %d steps, %d with candidates, %d skipped. Output: %s",
        len(results), candidates_found, skipped, output_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline AQRR backtest runner — feeds real evaluate_symbol() on historical candle data."
    )
    parser.add_argument("--symbol", required=True, help="Symbol (e.g., BTCUSDT)")
    parser.add_argument("--data-dir", default="data/historical", help="Directory containing CSV candle files")
    parser.add_argument("--output", required=True, help="Output JSON artifact path")
    parser.add_argument("--start", required=True, type=int, help="Start timestamp in milliseconds")
    parser.add_argument("--end", required=True, type=int, help="End timestamp in milliseconds")
    parser.add_argument("--step-bars", type=int, default=4, help="Evaluation interval in 15m bars (default: 4 = 1 hour)")
    args = parser.parse_args()

    run_backtest(
        symbol=args.symbol,
        data_dir=Path(args.data_dir),
        output_path=Path(args.output),
        start_ms=args.start,
        end_ms=args.end,
        step_bars=args.step_bars,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
