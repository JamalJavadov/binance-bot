from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_aqrr_validation_docs_exist() -> None:
    assert (REPO_ROOT / "docs" / "AQRR_VALIDATION_LADDER.md").exists()
    assert (REPO_ROOT / "docs" / "AQRR_MICRO_LIVE_READINESS.md").exists()
    assert (REPO_ROOT / "README.md").read_text(encoding="utf-8").find("AQRR Validation") != -1


def test_aqrr_validation_runner_exposes_backtest_and_micro_live_modes() -> None:
    runner = REPO_ROOT / "backend" / "scripts" / "aqrr_validation.py"
    result = subprocess.run(
        [sys.executable, str(runner), "backtest", "--symbol", "BTCUSDT"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["mode"] == "backtest"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["docs"]["validation_ladder"].endswith("AQRR_VALIDATION_LADDER.md")

    micro_live = subprocess.run(
        [sys.executable, str(runner), "micro-live"],
        check=True,
        capture_output=True,
        text=True,
    )
    micro_live_payload = json.loads(micro_live.stdout)

    assert micro_live_payload["mode"] == "micro-live"
