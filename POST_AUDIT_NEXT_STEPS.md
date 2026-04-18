# Post-Audit Next Steps: AQRR Bot Progression

## Phase 1: Repository Safety Check
During the recent compliance audit runs, the following actions and boundaries were observed:
- **Files Modified/Created**: 
  - `AQRR_STRATEGY_COMPLIANCE_AUDIT.md` (Created and Revised)
  - `tasks.md`, `implementation_plan.md`, `walkthrough.md` (Temporary `.gemini/antigravity` artifact bounds).
- **Commands Executed**: Read-only directory listings (`ls`) and strict regex file searches (`grep`) on the `/backend` logic files to prove implementation truth.
- **Git / GitHub State**: Untouched. No branches were created, no commits were authored, and no remote pulls/pushes occurred.
- **Operational Scope**: Completely respected. No application code, environments, credentials, config files, or binary environments were altered.

---

## Phase 2: Post-Audit Action Plan

The path to scaling production risk passes through two parallel tracks. Currently, we will **plan** these tracks without applying code changes.

### Track A: Documentation Correction

The `COMPLETE_PROJECT_DOCUMENTATION.md` file contains outdated architectural statements that undermine the strict mathematical reality of the python codebase. 

**Target 1: Section 19. Known Constraints & Risks (Line 1293)**
- **Current Text**: *- "Correlation filter — the spec describes a rolling correlation filter; the implementation uses lighter-weight thematic / beta clustering checks."*
- **Required Action**: Remove this bullet point entirely. The codebase actively implements `_correlation` in `aqrr.py` using a full rolling Pearson correlation math block.

**Target 2: Section 9.6. Candidate Ranking & Selection (Line 798)**
- **Current Text**: *"- Correlation conflict check (prevents three highly correlated altcoin longs, for example)"*
- **Required Action**: Expand this bullet to accurately reflect the active code: *"Rolling Pearson Correlation filter: actively computes correlation across the last 72 hours of 15m/1h returns (`returns_1h`) against existing open candidate positions, rejecting new trades that breach the `correlation_reject_threshold`."*

### Track B: Validation Pipeline Implementation

The Strategy Spec mandates a strict sequence for moving into live deployment: **Backtest -> Walk-Forward -> Paper -> Micro-Live**. The bot’s current script (`backend/scripts/aqrr_validation.py`) is merely a stub. 

**Architectural Boundary:**
- **Inside the Bot Repo**: The live `statistics.py` module ready to parse hit-rate bucket values, the live auto-mode trigger, and paper-trade (Testnet) endpoints if adapted.
- **External to the Bot Repo (CLI Tooling)**: The heavy backtesting engine. The historical K-Line data fetcher, the mock-exchange simulator, and the walk-forward slicing mechanism should be external CLI logic, generating output artifacts (JSON or SQL seed files) that the live bot can ingest.

**Concrete Implementation Steps:**

1. **Step 1: Historical Data Ingestion Tool**
   - Write a standalone script (e.g., `backend/scripts/historical_fetch.py`) to hit Binance API and download 2 years of 15m/1h OHLCV data into local CSVs or a dedicated Postgres testing database.

2. **Step 2: The Mock Backtest Engine**
   - Create a lightweight test harness that loops over the historical OHLCV data, bypassing `BinanceGateway` and feeding ticks directly into `ScannerService.run_scan(mocked_timestamp)`.
   - Use a basic touch-model to simulate limit order fills and capture PNL series.

3. **Step 3: Walk-Forward Calibration Generator**
   - Modify the backtest engine to use moving 6-month walk-forward slices.
   - Aggregate closed trades into the `aqrr_trade_stats` shape (`<setup_family>|<direction>|<market_state>|<score_band>|...`).
   - Export these populated buckets via Alembic migration or SQL seed so the bot's `calibrated_rank_value()` logic spins up with populated historical hit rates.

4. **Step 4: Paper Trading / Testnet Adapter**
   - Implement Binance Testnet endpoints. Switch `BINANCE_BASE_URL` based on an explicit `.env` toggle. Let the bot run in `auto_mode_enabled=True` locally without bridging real credentials.

5. **Step 5: Micro-Live Constraints**
   - Once paper trading validates execution bounds, scale up the `risk_per_trade_fraction` internally starting at the lowest possible decimal (e.g., $1 risk per trade) before jumping to 1-2%.
