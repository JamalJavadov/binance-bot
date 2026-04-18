#!/usr/bin/env python3
"""
walkforward_runner.py

Real walk-forward runner: iterates train/test windows from a validated config,
runs the offline backtest evaluation for each test window, and produces
SliceResult artifacts ready for bucket-stat export.

Usage:
  python backend/scripts/walkforward_runner.py \\
    --config data/validation_config.json \\
    --data-dir data/historical \\
    --output data/walkforward/slices.json

Config format (WalkForwardConfig schema):
  {
    "symbols": ["BTCUSDT"],
    "intervals": ["15m", "1h", "4h"],
    "periods": [
      {
        "train_start": 1640995200000,
        "train_end":   1656633600000,
        "test_start":  1656633600000,
        "test_end":    1672531200000
      }
    ],
    "min_bucket_samples": 20
  }
"""

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import argparse
import json
import logging
import time
from pathlib import Path

from pydantic import ValidationError

from app.models.enums import SignalDirection
from app.schemas.validation import SliceResult, WalkForwardConfig, BucketGeneratedStat, BucketExportArtifact
from app.services.strategy.config import resolve_strategy_config
from app.services.strategy.statistics import score_band_for_final_score, volatility_band_for_percentile
from app.validation.candle_loader import (
    MIN_15M_BARS, MIN_1H_BARS, MIN_4H_BARS,
    load_candles_from_csv,
    derive_1h_candles,
    derive_4h_candles,
    candles_up_to,
    candles_in_range,
)
from app.validation.step_evaluator import StepCandidate, run_step
from app.validation.simulator import simulate_execution

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BAR_MS = 15 * 60 * 1000
DEFAULT_STEP_BARS = 4  # evaluate every 1 hour
WINDOW_15M = MIN_15M_BARS + 20
WINDOW_1H = MIN_1H_BARS + 10
WINDOW_4H = MIN_4H_BARS + 10


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> WalkForwardConfig:
    """Load and validate a WalkForwardConfig JSON file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    try:
        config = WalkForwardConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid walk-forward config: {exc}") from exc

    _validate_periods(config)
    return config


def _validate_periods(config: WalkForwardConfig) -> None:
    """Validate temporal ordering of each train/test slice."""
    if not config.periods:
        raise ValueError("WalkForwardConfig must contain at least one period.")
    for idx, period in enumerate(config.periods):
        if period.train_start >= period.train_end:
            raise ValueError(f"Period {idx}: train_start must be before train_end.")
        if period.train_end > period.test_start:
            raise ValueError(f"Period {idx}: train_end cannot be after test_start (prevents lookahead).")
        if period.test_start >= period.test_end:
            raise ValueError(f"Period {idx}: test_start must be before test_end.")


# ---------------------------------------------------------------------------
# Per-slice backtest
# ---------------------------------------------------------------------------

def run_slice(
    *,
    symbol: str,
    period_index: int,
    test_start: int,
    test_end: int,
    train_start: int,
    train_end: int,
    all_15m,
    all_1h,
    all_4h,
    config,
    step_bars: int = DEFAULT_STEP_BARS,
    override_expiry_bars: int | None = None,
) -> SliceResult:
    """
    Evaluate the strategy over one TEST window of a walk-forward slice.

    The train window is included in this call for context only; it is NOT
    currently used in the evaluation (no in-sample parameter fitting is done
    in this phase — the strategy uses fixed default parameters).
    """
    step_ms = step_bars * BAR_MS
    eval_times = list(range(test_start, test_end, step_ms))

    all_candidates: list[dict] = []
    total_steps = 0
    steps_skipped = 0
    steps_with_candidates = 0

    for eval_time in eval_times:
        # Windows must reach back into training data to have enough history
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
        total_steps += 1
        if step.skipped:
            steps_skipped += 1
        elif step.candidates:
            steps_with_candidates += 1
            
            # Slice future candles exclusively after current eval_time_ms
            # Assuming 15m candles are sorted, we can use binary search or simple iteration.
            # all_15m is small enough for list comprehension in this iteration
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
                    expiry_bars=override_expiry_bars if override_expiry_bars is not None else candidate.expiry_bars,
                    future_candles=future_candles
                )
                
                candidate.sim_outcome = sim_res.outcome.value
                candidate.sim_fill_time_ms = sim_res.fill_time_ms
                candidate.sim_exit_time_ms = sim_res.exit_time_ms
                
                all_candidates.append(candidate.to_dict())

    return SliceResult(
        period_index=period_index,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        symbol=symbol,
        total_steps=total_steps,
        steps_skipped=steps_skipped,
        steps_with_candidates=steps_with_candidates,
        candidates=all_candidates,
    )


# ---------------------------------------------------------------------------
# Bucket-stat export pipeline
# ---------------------------------------------------------------------------

def build_bucket_export(slices: list[SliceResult]) -> BucketExportArtifact:
    """
    Aggregate candidate signals from all walk-forward slices into a
    BucketExportArtifact, grouped by canonical bucket key.

    Since fills are not simulated in this phase, closed_trade_count and
    win/loss counts are 0. The candidate_count field reflects how many times
    each bucket was triggered during the test windows.
    """
    from collections import defaultdict
    from app.models.enums import SignalDirection

    # bucket_key → {setup_family, direction, market_state, score_band, volatility_band, execution_tier, candidate_count}
    bucket_map: dict[str, dict] = defaultdict(lambda: {
        "setup_family": "",
        "direction": None,
        "market_state": "",
        "score_band": "",
        "volatility_band": "",
        "execution_tier": "",
        "candidate_count": 0,
        "closed_trade_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "gross_profit_r": 0.0,
        "gross_loss_r": 0.0,
    })

    for slice_result in slices:
        for cand_dict in slice_result.candidates:
            direction_enum = (
                SignalDirection.LONG if cand_dict["direction"] == "LONG" else SignalDirection.SHORT
            )
            score_band = score_band_for_final_score(cand_dict["final_score"])
            atr_pct = cand_dict.get("atr_percentile")
            volatility_band = volatility_band_for_percentile(
                float(atr_pct) if atr_pct is not None else None
            )
            # Execution tier from the candidate — normalise to lowercase
            execution_tier = str(cand_dict.get("execution_tier", "tier_a")).lower()

            # Build the canonical bucket key
            bucket_key = "|".join([
                cand_dict["setup_family"],
                cand_dict["direction"],
                cand_dict["market_state"],
                score_band,
                volatility_band,
                execution_tier,
            ])

            entry = bucket_map[bucket_key]
            entry["setup_family"] = cand_dict["setup_family"]
            entry["direction"] = direction_enum
            entry["market_state"] = cand_dict["market_state"]
            entry["score_band"] = score_band
            entry["volatility_band"] = volatility_band
            entry["execution_tier"] = execution_tier
            entry["candidate_count"] += 1

            sim_outcome = cand_dict.get("sim_outcome")
            if sim_outcome in ("WIN", "LOSS"):
                entry["closed_trade_count"] += 1
                if sim_outcome == "WIN":
                    entry["win_count"] += 1
                    entry["gross_profit_r"] += cand_dict.get("net_r_multiple", 0.0)
                elif sim_outcome == "LOSS":
                    entry["loss_count"] += 1
                    entry["gross_loss_r"] += 1.0

    buckets = [
        BucketGeneratedStat(
            setup_family=v["setup_family"],
            direction=v["direction"],
            market_state=v["market_state"],
            score_band=v["score_band"],
            volatility_band=v["volatility_band"],
            execution_tier=v["execution_tier"],
            closed_trade_count=v["closed_trade_count"],
            win_count=v["win_count"],
            loss_count=v["loss_count"],
            breakeven_count=0,
            gross_profit=round(v["gross_profit_r"], 4),
            gross_loss=round(v["gross_loss_r"], 4),
            candidate_count=v["candidate_count"],
        )
        for v in bucket_map.values()
    ]

    range_start = min(s.test_start for s in slices) if slices else 0
    range_end = max(s.test_end for s in slices) if slices else 0
    total_candidates = sum(s.steps_with_candidates for s in slices)

    return BucketExportArtifact(
        generated_at=int(time.time() * 1000),
        walk_forward_range_start=range_start,
        walk_forward_range_end=range_end,
        data_points=total_candidates,
        buckets=buckets,
        phase_notes=[
            "Phase 4: Simulated execution outcomes aggregated offline.",
            "gross_profit and gross_loss reflect normalized R-multiples, not USDT.",
            "Higher timeframe candles derived from 15m aggregation."
        ],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_walkforward(
    *,
    wf_config: WalkForwardConfig,
    data_dir: Path,
    output_dir: Path,
    step_bars: int = DEFAULT_STEP_BARS,
    override_expiry_bars: int | None = None,
) -> tuple[list[SliceResult], BucketExportArtifact]:
    """
    Run the full walk-forward pipeline for all symbols and periods defined in the config.
    Returns the list of SliceResults and the aggregated BucketExportArtifact.
    """
    ev_config = resolve_strategy_config({})
    all_slices: list[SliceResult] = []

    for symbol in wf_config.symbols:
        logger.info("Loading candles for %s", symbol)
        csv_path = data_dir / f"{symbol.upper()}_15m.csv"
        all_15m = load_candles_from_csv(csv_path)
        if not all_15m:
            logger.warning("No data for %s — skipping.", symbol)
            continue

        all_1h = derive_1h_candles(all_15m)
        all_4h = derive_4h_candles(all_15m)

        for idx, period in enumerate(wf_config.periods):
            logger.info(
                "Symbol %s | Period %d: test window [%d, %d]",
                symbol, idx, period.test_start, period.test_end,
            )
            slice_result = run_slice(
                symbol=symbol,
                period_index=idx,
                test_start=period.test_start,
                test_end=period.test_end,
                train_start=period.train_start,
                train_end=period.train_end,
                all_15m=all_15m,
                all_1h=all_1h,
                all_4h=all_4h,
                config=ev_config,
                step_bars=step_bars,
                override_expiry_bars=override_expiry_bars,
            )
            all_slices.append(slice_result)
            logger.info(
                "  → %d steps, %d with candidates, %d candidates total",
                slice_result.total_steps,
                slice_result.steps_with_candidates,
                len(slice_result.candidates),
            )

    artifact = build_bucket_export(all_slices)
    return all_slices, artifact


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Walk-forward runner: iterates train/test slices and produces bucket-stat artifacts."
    )
    parser.add_argument("--config", required=True, help="Path to WalkForwardConfig JSON")
    parser.add_argument("--data-dir", default="data/historical", help="Directory containing CSV candle files")
    parser.add_argument("--output", required=True, help="Output directory for slices and bucket artifact")
    parser.add_argument("--step-bars", type=int, default=DEFAULT_STEP_BARS, help="Evaluation interval in 15m bars")
    parser.add_argument(
        "--override-expiry-bars", type=int, default=None,
        help="Experiment: override simulated expiry_bars for all candidates (e.g. 16 = 240 min). Does not affect strategy logic."
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    wf_config = load_config(Path(args.config))
    slices, artifact = run_walkforward(
        wf_config=wf_config,
        data_dir=Path(args.data_dir),
        output_dir=output_dir,
        step_bars=args.step_bars,
        override_expiry_bars=args.override_expiry_bars,
    )

    # Write slices
    slices_path = output_dir / "walkforward_slices.json"
    with open(slices_path, "w", encoding="utf-8") as fh:
        json.dump([s.model_dump() for s in slices], fh, indent=2)
    logger.info("Slices written to %s", slices_path)

    # Write bucket artifact
    artifact_path = output_dir / "bucket_export.json"
    with open(artifact_path, "w", encoding="utf-8") as fh:
        fh.write(artifact.model_dump_json(indent=2))
    logger.info("Bucket export written to %s", artifact_path)
    logger.info(
        "Walk-forward complete: %d slices, %d buckets in export.",
        len(slices), len(artifact.buckets),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
