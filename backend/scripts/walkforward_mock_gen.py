#!/usr/bin/env python3
"""
walkforward_mock_gen.py

A standalone offline utility to manually generate a mock validation artifact.
This simulates the exact final output envelope that a production Offline 
Walk-Forward Simulator would piece together and export.

Usage:
  python backend/scripts/walkforward_mock_gen.py --output data/validation/mock_walkforward_artifact.json
"""

import sys
import os
# Add the project root to the python path so 'app' can be imported when running as a standalone script
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import argparse
import time
from pathlib import Path

from app.schemas.validation import BucketExportArtifact, BucketGeneratedStat
from app.models.enums import SignalDirection

def main():
    parser = argparse.ArgumentParser(description="Generate a mock validation bucket artifact offline")
    parser.add_argument(
        "--output", 
        default="data/validation/mock_walkforward_artifact.json",
        help="Target file path for the mock JSON output"
    )
    args = parser.parse_args()

    # Create a completely artificial offline bucket statistic
    # This precisely matches what would be produced by simulated slices
    mock_bucket = BucketGeneratedStat(
        setup_family="trend_continuation_breakout",
        direction=SignalDirection.LONG,
        market_state="bull_trend",
        score_band="80_89",
        volatility_band="normal",
        execution_tier="tier_a",
        closed_trade_count=45,
        win_count=23,
        loss_count=18,
        breakeven_count=4,
        gross_profit=63.5,
        gross_loss=-18.0
    )

    now_ms = int(time.time() * 1000)
    
    # Build the full validation artifact wrapper
    artifact = BucketExportArtifact(
        generated_at=now_ms,
        walk_forward_range_start=now_ms - 180 * 24 * 3600 * 1000, # 6 months ago mock date
        walk_forward_range_end=now_ms,
        data_points=845000,
        buckets=[mock_bucket]
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(out_path, "w", encoding="utf-8") as file_obj:
        # Strict Pydantic dump ensures it perfectly satisfies schema validation
        file_obj.write(artifact.model_dump_json(indent=2))

    print(f"✅ Mock validation artifact successfully generated offline at: {out_path}")

if __name__ == "__main__":
    main()
