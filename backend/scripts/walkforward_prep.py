#!/usr/bin/env python3
import sys
import os
# Add the project root to the python path so 'app' can be imported when running as a standalone script
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import argparse
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

from pydantic import ValidationError

from app.schemas.validation import WalkForwardConfig, WalkForwardPeriod, BucketExportArtifact, BucketGeneratedStat
from app.models.enums import SignalDirection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def validate_periods(periods: list[WalkForwardPeriod]) -> None:
    """Validate that train and test slices are chronologically logical."""
    if not periods:
        raise ValueError("WalkForwardConfig must contain at least one period.")
        
    for index, period in enumerate(periods):
        if period.train_start >= period.train_end:
            raise ValueError(f"Period {index}: train_start must be before train_end.")
        if period.train_end > period.test_start:
            raise ValueError(f"Period {index}: train_end cannot be after test_start (prevents lookahead).")
        if period.test_start >= period.test_end:
            raise ValueError(f"Period {index}: test_start must be before test_end.")


def load_and_validate_config(config_path: Path) -> WalkForwardConfig:
    """Loads a JSON config and validates it against the WalkForwardConfig schema."""
    logger.info(f"Loading configuration from {config_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
        
    try:
        with open(config_path, "r", encoding="utf-8") as file_obj:
            config_data = json.load(file_obj)
            
        config = WalkForwardConfig.model_validate(config_data)
        validate_periods(config.periods)
        logger.info(f"Successfully validated config with {len(config.symbols)} symbols and {len(config.periods)} periods.")
        return config
    except ValidationError as e:
        logger.error(f"Schema validation failed for {config_path}")
        logger.error(e)
        raise
    except json.JSONDecodeError as e:
        logger.error(f"Malformed JSON in {config_path}")
        raise


def prepare_empty_artifact(config: WalkForwardConfig) -> BucketExportArtifact:
    """
    Prepares a clean, empty BucketExportArtifact structure based on the walk forward bounds.
    This structure is ready to receive simulated backtest results offline.
    """
    # Range is defined across the very first train start to the very last test end
    range_start = min(p.train_start for p in config.periods)
    range_end = max(p.test_end for p in config.periods)
    
    # We initialize an empty list. The actual simulation engine will populate this list
    # with the performance across execution tiers, volatility bands, etc.
    # For preparation, we produce just the envelope.
    export_artifact = BucketExportArtifact(
        generated_at=int(datetime.now(timezone.utc).timestamp() * 1000),
        walk_forward_range_start=range_start,
        walk_forward_range_end=range_end,
        data_points=0,
        buckets=[]
    )
    
    return export_artifact


def export_artifact(artifact: BucketExportArtifact, output_path: Path) -> None:
    """Exports the initialized artifact structure to disk for the external simulator."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as file_obj:
        # Pydantic dump
        file_obj.write(artifact.model_dump_json(indent=2))
        
    logger.info(f"Successfully exported empty artifact schema to {output_path}")
    logger.info("This artifact is now ready to be consumed and populated by the offline full simulator.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-Forward Validation Preparation CLI. Loads configurations and initializes artifact envelopes offline.")
    parser.add_argument("--config", required=True, help="Path to the JSON walk-forward configuration")
    parser.add_argument("--output", required=True, help="Path to export the initialized BucketExportArtifact JSON")
    args = parser.parse_args()

    config_path = Path(args.config)
    output_path = Path(args.output)
    
    try:
        config = load_and_validate_config(config_path)
        blank_artifact = prepare_empty_artifact(config)
        export_artifact(blank_artifact, output_path)
    except Exception as e:
        logger.error(f"Walk-forward preparation aborted: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
