# AQRR Validation Implementation Plan

## Overview
This architectural document governs the sequence, bounds, and implementation strategy for executing historical validations, advancing through paper simulation, and deploying micro-live execution, fulfilling the canonical requirements of the AQRR Strategy Specification.

## Data and Execution Boundaries

To preserve safety and modularity, the architectural lines are drawn explicitly:

1. **Internal (Inside Bot Repo)**
   - Paper Trading limits (Testnet URL routing)
   - Micro-Live scaling logic (fractional risk configs)
   - Walk-forward calibration hooks (`statistics.py` import logic processing static result schemas)
   
2. **External (Outside / CLI Tooling Layer)**
   - Historical Binance Kline ingestion
   - Offline looping simulator mimicking `ScannerService.run_scan(timestamp)`
   - Walk-forward logic slicer generating performance buckets

*Why?* The bot evaluates active market structures. Wrapping 2 years of simulated historical progression inside the live FastAPI web loop invites unmanageable risk and state leakages. Backtesting logic belongs offline. 

---

## The Four Stages of Readiness

### Stage 1: Backtest Foundation (Offline CLI)
**Mandate**: Verify fundamental profitability and filter accuracy on continuous 15m/1h tick data.

**Requirements**:
- **Data Ingestion**: A dedicated tool fetching and caching Binance `fapi/v1/klines` seamlessly. Outputting CSVs or a dedicated offline Postgres DB.
- **Engine Scaffold**: A loop iterating through timestamps, isolating `candles_15m` arrays up to `$time`, bypassing network requests, and injecting them into the `aqrr.py` evaluation tree.

### Stage 2: Walk-Forward Automation (Offline CLI)
**Mandate**: Mitigate curve-fitting by proving the system maintains `net_R >= 3.0` expectancies on sequentially advancing unseen data horizons.

**Requirements**:
- **Strategy Run**: Engine loops over 6-month in-sample windows. The metrics generate the `<setup_family>|<direction>|<market_state>` bucket hit rates.
- **Export Schema**: A standardized json payload or `.sql` batch generated matching `AqrrTradeStat` logic.

### Stage 3: Paper Simulation (Internal Wrapper)
**Mandate**: Ensure execution limits, buffers, and latency handles match real order-book reality without committing live equity.

**Requirements**:
- **Testnet Adapter**: `BINANCE_BASE_URL` switched to testnet endpoints locally.
- **Auto-mode Enabled**: The local scheduler loops unhindered. No changes required to strategy or configuration aside from the URL base toggle in `.env`.

### Stage 4: Micro-Live Constraints (Internal Governance)
**Mandate**: Introduce real equity at irreducible minimum margins.

**Requirements**:
- Establish `< 0.20%` fractional limits inside `StrategyConfig` parameters.
- Verify real taker/maker commissions match estimations in `_estimated_cost_distance`.
