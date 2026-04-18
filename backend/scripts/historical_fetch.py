#!/usr/bin/env python3
import argparse
import asyncio
import csv
import logging
from pathlib import Path
from typing import Any

import httpx

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Constants
BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"

async def fetch_klines(client: httpx.AsyncClient, symbol: str, interval: str, start_time: int, end_time: int, limit: int = 1500) -> list[list[Any]]:
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "startTime": start_time,
        "endTime": end_time,
        "limit": limit
    }
    response = await client.get(BINANCE_URL, params=params)
    response.raise_for_status()
    return response.json()

async def download_history(symbol: str, interval: str, start_time: int, end_time: int, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"{symbol.upper()}_{interval}.csv"
    
    logger.info(f"Downloading {symbol} {interval} Klines to {filename}")
    
    current_start = start_time
    total_fetched = 0
    
    async with httpx.AsyncClient() as client:
        with open(filename, mode='w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_asset_volume", "number_of_trades", "taker_buy_base", "taker_buy_quote", "ignore"])
            
            while current_start < end_time:
                try:
                    klines = await fetch_klines(client, symbol, interval, current_start, end_time)
                    if not klines:
                        break
                    
                    writer.writerows(klines)
                    total_fetched += len(klines)
                    
                    # Advance time pointers. Binance returns times in milliseconds.
                    last_close_time = int(klines[-1][6])
                    current_start = last_close_time + 1
                    
                    logger.info(f"Fetched {len(klines)} rows. Total: {total_fetched}")
                    # Basic rate limit etiquette
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Failed fetching batch ending at {current_start}: {e}")
                    break
                    
    logger.info(f"Complete. {total_fetched} total records saved.")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch historical Klines from Binance USD-M Futures for validation workflows.")
    parser.add_argument("--symbol", required=True, help="Trading pair symbol (e.g., BTCUSDT)")
    parser.add_argument("--interval", required=True, choices=["15m", "1h", "4h"], help="Kline interval")
    parser.add_argument("--start", required=True, type=int, help="Start timestamp in milliseconds")
    parser.add_argument("--end", required=True, type=int, help="End timestamp in milliseconds")
    parser.add_argument("--outdir", default="data/historical", help="Output directory path (defaults to data/historical)")
    return parser.parse_args()

def main() -> None:
    args = parse_args()
    asyncio.run(download_history(args.symbol, args.interval, args.start, args.end, Path(args.outdir)))

if __name__ == "__main__":
    main()
