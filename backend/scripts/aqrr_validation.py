#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"


def _command_payload(mode: str, *, symbol: str | None, input_path: str | None, output_path: str | None) -> dict[str, object]:
    return {
        "mode": mode,
        "symbol": symbol,
        "input_path": input_path,
        "output_path": output_path,
        "docs": {
            "validation_ladder": str(DOCS_DIR / "AQRR_VALIDATION_LADDER.md"),
            "micro_live_readiness": str(DOCS_DIR / "AQRR_MICRO_LIVE_READINESS.md"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AQRR validation ladder scaffold for backtest, walk-forward, paper, and micro-live readiness.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("backtest", "walk-forward", "paper", "micro-live"):
        subparser = subparsers.add_parser(mode, help=f"Emit the AQRR {mode} validation scaffold.")
        subparser.add_argument("--symbol", help="Optional symbol focus for the validation run.")
        subparser.add_argument("--input", dest="input_path", help="Optional input dataset or config path.")
        subparser.add_argument("--output", dest="output_path", help="Optional output directory or report path.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    payload = _command_payload(
        args.mode,
        symbol=args.symbol,
        input_path=args.input_path,
        output_path=args.output_path,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
