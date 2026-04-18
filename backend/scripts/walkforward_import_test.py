#!/usr/bin/env python3
"""
walkforward_import_test.py

A manual import and testing workflow that inserts offline generated validation artifacts
into the local database via the `import_walk_forward_buckets` hook.

Zero live trading logic is triggered. This merely verifies that the database bridging
mechanic operates as intended for future real validation pipelines.

Usage:
  python backend/scripts/walkforward_import_test.py --input data/validation/mock_walkforward_artifact.json
"""

import sys
import os
# Add the project root to the python path so 'app' can be imported when running as a standalone script
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import argparse
import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.aqrr_trade_stat import AqrrTradeStat
from app.services.strategy.statistics import import_walk_forward_buckets
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def run_manual_import_test(json_path: Path, confirm_nonprod: bool):
    # DANGEROUS BOUNDARY CHECK
    # Strictly enforce that offline bridging handles test databases ONLY.
    settings = get_settings()
    db_name = str(settings.database_url).lower()
    if "validation" not in db_name and "test" not in db_name:
        logger.critical(f"OPERATION ABORTED! Target Database '{db_name}' appears to be live.")
        logger.critical("Offline tests MUST use a separate database (e.g., aqrr_validation). Load `.env.validation.example`.")
        return
        
    if not confirm_nonprod:
        logger.critical("OPERATION ABORTED! You must explicitly pass the `--confirm-nonprod` flag to write validation metrics.")
        return
    if not json_path.exists():
        logger.error(f"Artifact not found at {json_path}. Please run `walkforward_mock_gen.py` first.")
        return

    logger.info(f"Reading validation schema from {json_path}...")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.error(f"Failed parsing JSON: {e}")
        return

    logger.info("Initializing offline database connection wrapper to exercise statistics bridging...")
    async with AsyncSessionLocal() as session:
        try:
            # Safely exercise the isolated statistics hook. 
            # This logic explicitly does not touch any order management or trading mechanics.
            await import_walk_forward_buckets(session, export_payload=payload)
            await session.commit()
            
            logger.info("Import hook successfully committed artifact structures to database.")
            
            # Post-import verification: Fetch current DB state for AqrrTradeStat
            rows = (await session.execute(select(AqrrTradeStat))).scalars().all()
            
            logger.info(f"Verification Success! There are now {len(rows)} calibrated buckets residing in the db:")
            
            for row in rows:
                hit_rate = (row.win_count / row.closed_trade_count * 100) if row.closed_trade_count > 0 else 0
                logger.info(
                    f" - Key: {row.bucket_key} | Trades: {row.closed_trade_count} | Wins: {row.win_count} | Estimated Hit Rate: {hit_rate:.1f}%"
                )
                
        except Exception as e:
            await session.rollback()
            logger.error(f"Database bridging hook failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="Test manual import of an offline bucket artifact securely into the local postgres stat tables.")
    parser.add_argument(
        "--input", 
        default="data/validation/mock_walkforward_artifact.json", 
        help="Path to the BucketExportArtifact json artifact"
    )
    parser.add_argument(
        "--confirm-nonprod",
        action="store_true",
        help="Hard confirmation that the target `.env` Database URL is safely isolated from production."
    )
    args = parser.parse_args()
    
    asyncio.run(run_manual_import_test(Path(args.input), args.confirm_nonprod))

if __name__ == "__main__":
    main()
