# Adaptive Quality-Ranked Regime Strategy (AQRR)
### Full Strategy Specification for Binance USDⓈ-M Futures Automated Trading Bot

---

> **Disclaimer:** This document is produced for engineering and strategy design purposes only. It does not constitute financial advice. Futures trading involves substantial risk of loss, including the risk of liquidation. All parameter values marked as *starter defaults* must be validated through rigorous backtesting and paper trading before live deployment.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Why AQRR Was Selected](#2-why-aqrr-was-selected)
3. [Strategy Architecture Overview](#3-strategy-architecture-overview)
4. [Universe and Symbol Selection](#4-universe-and-symbol-selection)
5. [Market Condition Filters](#5-market-condition-filters)
6. [Multi-Timeframe Feature Set](#6-multi-timeframe-feature-set)
7. [Regime Classifier](#7-regime-classifier)
8. [Signal Modules](#8-signal-modules)
   - 8.1 [Trend Module — Breakout Candidates](#81-trend-module--breakout-candidates)
   - 8.2 [Trend Module — Pullback Candidates](#82-trend-module--pullback-candidates)
   - 8.3 [Range Module — Mean Reversion Candidates](#83-range-module--mean-reversion-candidates)
9. [Trade Plan Construction (Entry / Stop / TP)](#9-trade-plan-construction-entry--stop--tp)
10. [Quality Scoring System](#10-quality-scoring-system)
11. [Cross-Symbol Ranking](#11-cross-symbol-ranking)
12. [Correlation and Diversification Filter](#12-correlation-and-diversification-filter)
13. [Final Opportunity Selection (0–3 Trades)](#13-final-opportunity-selection-03-trades)
14. [Position Sizing and Capital Allocation](#14-position-sizing-and-capital-allocation)
15. [Leverage Selection Logic](#15-leverage-selection-logic)
16. [Order Entry and Order Types](#16-order-entry-and-order-types)
17. [Pending Order Expiry Logic](#17-pending-order-expiry-logic)
18. [Open Position Lifecycle Management](#18-open-position-lifecycle-management)
19. [Stop-Loss Management](#19-stop-loss-management)
20. [Take-Profit Management](#20-take-profit-management)
21. [Re-entry Policy](#21-re-entry-policy)
22. [Risk Controls and Circuit Breakers](#22-risk-controls-and-circuit-breakers)
23. [Exchange Reality Constraints](#23-exchange-reality-constraints)
24. [Full Pseudocode — Scan and Execution Engine](#24-full-pseudocode--scan-and-execution-engine)
25. [Parameter Reference Table](#25-parameter-reference-table)
26. [Backtest Design Requirements](#26-backtest-design-requirements)
27. [Performance Metrics to Report](#27-performance-metrics-to-report)
28. [Implementation Checklist](#28-implementation-checklist)
29. [Known Limitations and Open Decisions](#29-known-limitations-and-open-decisions)

---

## 1. Executive Summary

The **Adaptive Quality-Ranked Regime Strategy (AQRR)** is a fully automated trading engine designed to scan the entire active Binance USDⓈ-M perpetual futures market, identify only the strongest and most realistic tradable setups available at any given moment, and execute up to three of them — or none if the market does not justify entry.

AQRR is not a single rigid trading style. It is an intelligent **selector**: it detects the market regime for each symbol (trending or ranging), applies the appropriate signal module, constructs a full trade plan with a minimum 1:3 risk-to-reward structure, scores each candidate on a multi-factor quality scale, ranks all candidates cross-symbol, applies a correlation filter to prevent concentrated exposure, and finally submits only the top 0–3 highest-quality, diversified setups.

The strategy is designed specifically for:
- A **10 USD small account** on Binance USDⓈ-M perpetual futures
- A **fully automated, end-to-end execution flow** (user starts the bot; the bot handles everything)
- **Quality over quantity** — no forced trades, no activity for activity's sake
- A **hard minimum 1:3 R:R** on every trade
- **Equal capital allocation** across all active positions

---

## 2. Why AQRR Was Selected

Multiple strategy families were evaluated against the full requirement brief. The shortlist and their key weaknesses relative to the requirements are summarised below:

| Strategy Family | Primary Disqualifying Factor |
|---|---|
| Pure Trend Following (TSM) | Not adaptive; struggles in range conditions; no regime awareness |
| Cross-Sectional Momentum | Tends to force trades by always ranking; no natural "0 trade" output without extra gates |
| Pure Mean Reversion | High turnover; very difficult to achieve 1:3 R:R on modest reversion moves |
| Pairs Trading | Requires 2 legs per trade; consumes 2 of 3 position slots; impractical at 10 USD |
| Statistical Arbitrage | Needs large portfolios; highly sensitive to costs; not viable at 3 max positions |
| Market Making | Directly contradicts "quality over quantity"; high cost and latency sensitive |
| Carry / Funding Capture | Not a structured R:R setup; incompatible with 1:3 minimum hard rule |

**AQRR was selected because it is the only approach that simultaneously satisfies:**
- Adaptiveness (regime-aware, not one rigid style)
- Natural "0 trade is valid" output
- A hard 1:3 R:R gate built into candidate generation
- Ranking by quality and probability of success
- Correlation control over the selected set
- Full automation compatibility
- Practical viability on a 10 USD small account with Binance execution constraints

---

## 3. Strategy Architecture Overview

The AQRR engine runs on a repeating scan cycle. Each cycle passes through the following stages in strict order:

```
┌──────────────────────────────────────────────────────────────────────────┐
│  STAGE 1: Universe Loading                                               │
│  Load all active Binance USDⓈ-M symbols via /fapi/v1/exchangeInfo        │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 2: Symbol-Level Filters                                           │
│  Liquidity filter → spread filter → volatility shock filter              │
│  Symbols that fail are removed from consideration for this cycle         │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 3: Multi-Timeframe Feature Computation                            │
│  For each remaining symbol: compute features on 15m, 1h, 4h             │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 4: Regime Classification                                          │
│  Per symbol: is the 1h context trending or ranging?                     │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
            ┌────────────────────┴────────────────────┐
            │                                         │
┌───────────▼───────────┐              ┌──────────────▼────────────────┐
│  Trend Module         │              │  Range Module                 │
│  Breakout & Pullback  │              │  Mean Reversion Candidates    │
│  Candidates (L & S)   │              │  (only if 3R is feasible)     │
└───────────┬───────────┘              └──────────────┬────────────────┘
            └────────────────────┬────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 5: Trade Plan Construction                                        │
│  Entry price / Stop-loss / Take-profit (≥ 3R) for each candidate        │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 6: Quality Scoring                                                │
│  Multi-factor score [0–100] per candidate                               │
│  Candidates below quality threshold are discarded                        │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 7: Cross-Symbol Ranking                                           │
│  All surviving candidates sorted by score descending                    │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 8: Correlation / Diversification Filter                           │
│  Select top candidates that pass inter-trade correlation limits          │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼─────────────────────────────────────────┐
│  STAGE 9: Decision Gate                                                  │
│  0 candidates → no trade this cycle                                     │
│  1–3 candidates → size, leverage, place orders, set expiry, manage      │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Universe and Symbol Selection

### 4.1 Source

The full list of active Binance USDⓈ-M perpetual futures symbols is loaded at the start of each scan cycle via:

```
GET /fapi/v1/exchangeInfo
```

Filter: include only symbols where `status == "TRADING"`. Do not hard-code a fixed list. The Binance API is the only authoritative source.

### 4.2 Liquidity Filters

Every symbol must pass the following checks before it is considered for analysis:

| Filter | Logic | Reason |
|---|---|---|
| Minimum 24h quote volume | 24h volume in USDT ≥ configurable threshold (starter: 10,000,000 USDT) | Avoid illiquid symbols where small orders move price |
| Maximum relative spread | (best ask − best bid) / mid ≤ 0.10% | Prevent entry/exit costs from dominating on tiny account |
| Minimum price | Not near zero (eliminates micro-cap tokens with extreme tick noise) | Execution precision |
| Delist / status risk | Skip symbols with recent parameter changes or in wind-down phase | Avoid sudden margin changes |

> These thresholds are **starter defaults** and must be tuned based on your backtest results and observed execution reality.

### 4.3 Dynamic Configuration

Binance may change trading parameters (tick size, lot size, minimum notional, margin tier thresholds) at any time. The bot must treat the following as dynamic and re-fetch at least once per hour:

- `LOT_SIZE` filter (min qty, max qty, step size)
- `PRICE_FILTER` (tick size)
- `MIN_NOTIONAL` filter
- `MARKET_LOT_SIZE` filter

Never hard-code these values.

---

## 5. Market Condition Filters

These filters protect the engine from entering trades during conditions that materially reduce execution reliability or setup quality.

### 5.1 Volatility Shock Filter

```
atr_pct_now = ATR(14, 15m) / close_price

atr_pct_percentile = percentile_rank(atr_pct_now, last_7_days_of_atr_pct)

if atr_pct_percentile > 90th_percentile:
    skip symbol this cycle  # extreme volatility / pump-dump regime
```

This prevents entering during sudden price explosions or crashes where spreads widen, fills become unpredictable, and setups lose structural validity.

### 5.2 Spread Anomaly Filter

```
spread_now = (ask - bid) / mid
spread_7d_median = median(spread_observations, last_7_days)

if spread_now > 3.0 × spread_7d_median:
    skip symbol this cycle  # execution degraded
```

### 5.3 Funding Rate Sanity Filter

Extreme funding rates signal crowded positioning and create adverse holding costs.

```
funding_rate = GET /fapi/v1/premiumIndex → lastFundingRate

if abs(funding_rate) > 0.15%:    # tunable threshold
    avoid new position entry in the funding direction
    # e.g., avoid new longs if funding is extremely positive (longs pay shorts)
```

Funding mechanics are documented by Binance and applied every 8 hours on standard perpetual contracts. The cost is: `funding_fee = position_notional × funding_rate`. This cost can dominate a small account if funding is extreme and the position is held through a funding timestamp.

---

## 6. Multi-Timeframe Feature Set

For each symbol that passes Stage 2, the following features are computed across three timeframes.

### 6.1 Timeframe Structure

| Timeframe | Role | Lookback Required |
|---|---|---|
| 15m | Signal generation (entry, stop, TP) | Last 200 bars |
| 1h | Regime classification and trend context | Last 200 bars |
| 4h | Higher-context trend filter (optional but recommended) | Last 100 bars |

### 6.2 Feature List

**Trend and direction features (1h and 4h):**
- EMA(20), EMA(50), EMA(200) on 1h close
- ADX(14) on 1h (Average Directional Index — regime signal)
- +DI(14) and −DI(14) on 1h (directional indicators)
- Price position relative to EMA200 (above = bullish bias, below = bearish)
- 1h momentum: `close_now / close_48_bars_ago − 1` (48 × 15m ≈ 12h)

**Volatility features (15m):**
- ATR(14) on 15m
- ATR% = ATR(14) / close (normalised)
- Percentile rank of ATR% vs last 7 days

**Structure features (15m and 1h):**
- Rolling 20-bar high and low (resistance and support bands) on 15m
- Rolling 20-bar high and low on 1h
- Distance from price to key structure levels

**Momentum confirmation (15m):**
- RSI(14) on 15m
- Volume ratio: `current_bar_volume / average_volume(last_20_bars)`

**Spread and execution features:**
- Current best bid/ask spread (from bookTicker endpoint)
- 7-day median spread (rolling computation)

---

## 7. Regime Classifier

The regime classifier decides, per symbol, whether the current 1h environment is **trending** or **ranging**. This determines which signal module is activated.

### 7.1 Classification Rules

```
ADX(14) from 1h candles:

if ADX >= 25:
    regime = TREND        # clear directional regime

elif ADX >= 20 and ADX < 25:
    regime = TREND        # moderate trend, apply standard trend rules

elif ADX > 15 and ADX < 20:
    regime = UNCERTAIN    # borderline; only highest-quality trend setups allowed

elif ADX <= 15:
    regime = RANGE        # low directional strength; range module activated
```

### 7.2 Regime-to-Module Mapping

| Regime | Module Activated | Special Conditions |
|---|---|---|
| TREND | Trend Module (breakout + pullback) | Standard quality threshold applies |
| UNCERTAIN | Trend Module (breakout only) | Quality threshold raised to 80/100 |
| RANGE | Range Module (mean reversion) | 3R feasibility gate is mandatory |

The UNCERTAIN regime produces fewer candidates by design. This is intentional — the strategy must not force trades when regime conviction is low.

---

## 8. Signal Modules

### 8.1 Trend Module — Breakout Candidates

**Applicable regime:** TREND or UNCERTAIN

**Long breakout candidate:**
1. **Context filter:** 1h close is above EMA200 AND 1h momentum is positive (`close_now > close_48h_ago`)
2. **4h filter (optional):** 4h price is above 4h EMA50 (confirms macro trend direction)
3. **Setup:** 15m close breaks and closes above the 20-bar resistance high (a fresh breakout)
4. **Entry:** Limit order placed at the breakout level (the broken resistance, now expected support on retest)
   - This is a *retest entry*, not a chase entry. The bot waits for price to come back to the breakout level.
5. **Stop:** Below the last 15m swing low before the breakout (structural stop), minus `0.5 × ATR(14, 15m)` buffer
6. **Take profit:** `TP = entry + 3 × (entry − stop)` (hard 3R minimum)

**Short breakout candidate:** Symmetric (below EMA200, momentum negative, break below 20-bar support low).

### 8.2 Trend Module — Pullback Candidates

**Applicable regime:** TREND only (not UNCERTAIN)

**Long pullback candidate:**
1. **Context filter:** Same as breakout (above EMA200, positive 1h momentum)
2. **Setup:** 15m price pulls back into the EMA20–EMA50 zone after an established uptrend leg
3. **Confirmation:** A bullish rejection candle forms at the EMA zone (e.g., hammer, bullish engulf, or a close back above EMA20 after dipping below)
4. **Entry:** Limit order near the low of the rejection candle or at EMA20
5. **Stop:** Below the pullback swing low, minus `0.3 × ATR(14, 15m)` buffer
6. **Take profit:** `TP = entry + 3 × (entry − stop)` (hard 3R minimum)

**Short pullback candidate:** Symmetric (below EMA200, negative momentum, pullback into EMA zone, bearish rejection).

### 8.3 Range Module — Mean Reversion Candidates

**Applicable regime:** RANGE only

**Critical rule:** A range candidate is only generated if the structural distance from entry to the mean (midpoint of range) is **at least 3× the planned stop distance**. If this condition cannot be met, the candidate is rejected entirely. No partial 3R candidates are allowed.

**Long range mean reversion candidate:**
1. **Regime confirmation:** ADX(14, 1h) ≤ 15 (confirmed range)
2. **Range identification:** Define the current range high and range low from the last 20–40 1h bars
3. **Setup:** Price touches or slightly penetrates the range support, AND RSI(14, 15m) is below 30 (momentum exhaustion), AND the last completed 15m candle closes above the support level (stabilisation confirmation)
4. **3R feasibility gate:** `(range_midpoint − entry) ≥ 3 × (entry − stop_below_support)`; if this is false, reject candidate
5. **Entry:** Limit order near the range support
6. **Stop:** Below range support, minus small ATR buffer
7. **Take profit:** Range midpoint (if ≥ 3R), or full range high (if > 3R and realistically reachable)

**Short range mean reversion candidate:** Symmetric (range resistance, RSI > 70, bearish stabilisation candle, 3R feasibility required).

---

## 9. Trade Plan Construction (Entry / Stop / TP)

Every candidate, regardless of which module generated it, must have a fully defined trade plan before it enters scoring.

The trade plan contains:
- **Entry price:** the exact price at which the limit or stop order will be placed
- **Stop price:** the price at which the position is considered invalid and must be closed
- **Take-profit price:** the price at which the minimum reward target is reached
- **R-distance:** `abs(entry − stop)` in price units
- **TP distance:** `abs(tp − entry)` must be `≥ 3 × R-distance`
- **Planned R:R:** `tp_distance / r_distance` (must be ≥ 3.0; otherwise reject)
- **Direction:** LONG or SHORT

### 9.1 Stop Placement Principles

- Stops are always **structural**: placed beyond a swing point, breakout level, or range boundary — not at an arbitrary fixed distance
- Stops must have a volatility buffer of at minimum `0.3 × ATR(14, 15m)` to avoid premature stop-outs from normal noise
- Stops must **not** be widened simply to make the setup appear viable if the true structural stop would make 3R impossible
- If the structural stop is so wide that a realistic TP cannot be set, the candidate is rejected

### 9.2 Take-Profit Placement Principles

- TP is placed at exactly the price corresponding to `3R minimum`: `entry + 3 × R-distance` (long) or `entry − 3 × R-distance` (short)
- TP must not be placed beyond a major structural resistance (long) or support (short) that would realistically block price before reaching the target
- If a major structure blocks 3R, the candidate is rejected

---

## 10. Quality Scoring System

Each candidate receives a composite score from 0 to 100. Only candidates with a score **≥ 70** are passed to the ranking stage. This threshold is the "realistic quality gate" — it eliminates noise and weak setups while not requiring impossible perfection.

### 10.1 Scoring Components

| Component | Max Points | What It Measures |
|---|---|---|
| Trend alignment score | 25 | How cleanly the 15m direction aligns with 1h and 4h context |
| Momentum confirmation | 15 | Is momentum confirming direction at entry (e.g., RSI, volume ratio) |
| Structure quality | 20 | How clean and well-defined the key level is (consolidation depth, number of touches) |
| Volume confirmation | 10 | Is volume expanding in the breakout direction or confirming reversal |
| Volatility stability | 10 | Is volatility at a normal, executable level (penalise shock conditions) |
| Spread / execution quality | 10 | Is the current spread tight enough that entry and exit costs are manageable |
| R:R quality | 10 | Is the planned R:R above 3.0 (bonus for 4R, 5R+ setups) |

### 10.2 Scoring Rules

```
score = 0

# Trend alignment (25 pts)
if 15m direction matches 1h EMA context:  score += 15
if 15m direction matches 4h EMA context:  score += 10

# Momentum (15 pts)
if RSI confirms direction (e.g., RSI > 50 for long, < 50 for short): score += 7
if volume_ratio > 1.5 at signal bar:  score += 8

# Structure quality (20 pts)
if structure level has 3+ prior price touches: score += 10
if consolidation before breakout is clean (narrow ATR): score += 10

# Volume (10 pts)
if breakout/rejection bar volume > 1.3 × 20-bar average: score += 10

# Volatility stability (10 pts)
atr_pct_percentile = percentile_rank(ATR%, 7 days)
if atr_pct_percentile < 60th: score += 10
elif atr_pct_percentile < 75th: score += 5
else: score += 0

# Spread / execution (10 pts)
if spread_now < 0.5 × spread_7d_median: score += 10
elif spread_now < spread_7d_median: score += 5
else: score += 0

# R:R quality (10 pts)
if planned_rr >= 5.0: score += 10
elif planned_rr >= 4.0: score += 7
elif planned_rr >= 3.0: score += 5   # minimum; already required
else: score = 0  # should never reach here due to earlier gate

FINAL_SCORE = score   # [0, 100]
```

### 10.3 Quality Threshold Decision

The starter quality threshold is **70 / 100**. This means:

- A candidate that aligns with trend, has confirmed momentum, clean structure, and a tight spread will reliably pass
- A candidate that has only partial confirmation will not pass
- No candidate can pass on structure quality alone without directional confirmation

If backtesting reveals the bot trades too frequently, raise the threshold toward 75–80.
If backtesting reveals the bot almost never trades, lower the threshold toward 65 and investigate which component is consistently failing.

---

## 11. Cross-Symbol Ranking

After scoring, all surviving candidates across all symbols are placed into a single ranked list, sorted by score in descending order.

```
ranked_candidates = sorted(all_candidates, key=lambda c: c.score, descending=True)
```

This is a pure meritocratic ranking. A high-scoring BTCUSDT candidate and a high-scoring SOLUSDT candidate compete on equal terms.

The ranking reflects: trend strength, momentum conviction, structure quality, execution viability — not market cap, not volatility, not volume rank alone.

---

## 12. Correlation and Diversification Filter

**Hard requirement:** The bot must not open 3 positions that are essentially the same exposure.

### 12.1 Correlation Measurement

Compute the Pearson correlation of 1h returns between each pair of candidate symbols over the last 3–7 days of 1h bars:

```
for each pair (symbol_A, symbol_B):
    returns_A = pct_change(close_1h, symbol_A, last_72_bars)   # 3 days of 1h
    returns_B = pct_change(close_1h, symbol_B, last_72_bars)
    corr(A, B) = pearson_correlation(returns_A, returns_B)
```

### 12.2 Diversification Selection Algorithm

```
selected = []

for candidate in ranked_candidates:
    if len(selected) == 3:
        break
    
    is_correlated = False
    for already_selected in selected:
        if abs(corr(candidate.symbol, already_selected.symbol)) > 0.70:
            is_correlated = True
            break
    
    if not is_correlated:
        selected.append(candidate)

# selected now contains 0, 1, 2, or 3 diversified, quality-ranked candidates
```

The correlation threshold of **0.70** is a starter default. Raise to 0.75 if the bot struggles to find 3 uncorrelated candidates. Lower to 0.65 for stricter diversification.

### 12.3 Additional Diversification Rules

- Do not hold 3 simultaneous LONG positions on BTC, ETH, and a BTC-correlated alt. Even if correlation is measured below 0.70 on a given day, sector proximity creates concentration risk.
- Prefer selecting at least one candidate from a different market category if available (e.g., Layer 1, DeFi, Meme, AI tokens).
- Do not take two positions in the same direction on the same underlying theme during a macro risk-off event.

---

## 13. Final Opportunity Selection (0–3 Trades)

After the correlation filter, the `selected` list contains the final set of trades to execute in this scan cycle.

### 13.1 Decision Table

| Count in `selected` | Action |
|---|---|
| 0 | Skip this cycle entirely. No orders placed. Log: "No qualifying setups found." |
| 1 | Execute 1 trade. Full per-trade risk budget applied to this single trade. |
| 2 | Execute 2 trades. Risk budget divided equally between the two. |
| 3 | Execute 3 trades. Risk budget divided equally among all three. |

**The bot never forces the count to 3.** If fewer than 3 setups qualify, fewer are taken. This is a core design principle.

### 13.2 Existing Position Awareness

Before executing new trades, the bot checks:
- Current open positions (via `GET /fapi/v2/positionRisk`)
- Current open pending orders (via `GET /fapi/v1/openOrders`)
- Combined count must not exceed 3

If 2 positions are already open, a maximum of 1 new trade may be added in this cycle. If 3 are open, no new trades are added until a slot opens.

---

## 14. Position Sizing and Capital Allocation

### 14.1 Core Principle

Sizing is **risk-based** (not notional-based). Each trade is sized so that the monetary loss if the stop-loss is hit equals the per-trade risk budget.

```
per_trade_risk_usd = equity_usd × risk_pct_per_trade / number_of_selected_trades
```

For equal allocation across 1, 2, or 3 active trades, the total risk budget is divided equally.

### 14.2 Risk Percentage

- **Starter default:** 1.0% of equity per trade
- Example with 10 USD equity and 3 trades: `10 × 0.01 / 3 = 0.033 USD per trade`
- This is extremely small. Realistic execution on 10 USD may be constrained by Binance minimum notional requirements. See Section 23.

### 14.3 Position Size Formula

```
r_distance_price = abs(entry_price − stop_price)

quantity_raw = per_trade_risk_usd / r_distance_price

# Account for leverage:
quantity_leveraged = quantity_raw × leverage  (if not already included)

# Round to valid step size (from LOT_SIZE filter):
quantity = floor(quantity_raw / step_size) × step_size
```

Verify that `quantity × entry_price ≥ min_notional` (from MIN_NOTIONAL filter). If it does not, either increase leverage (within limits) or skip the trade as non-executable at current account size.

---

## 15. Leverage Selection Logic

Leverage is **not fixed**. The bot selects leverage automatically for each trade based on the following process:

### 15.1 Leverage Calculation

```
target_leverage = per_trade_risk_usd / (equity_per_trade × r_distance_pct)
```

Where `r_distance_pct = r_distance_price / entry_price`.

### 15.2 Leverage Constraints

| Constraint | Rule |
|---|---|
| Minimum leverage | 1× (no leverage) |
| Recommended maximum | 10× for small accounts (starter cap; adjust in backtest) |
| Hard exchange maximum | Per symbol from Binance leverage brackets |
| Minimum notional compliance | Leverage must be high enough that `qty × entry ≥ min_notional` |
| Liquidation buffer | Estimated liquidation price must be further from entry than the stop-loss |

### 15.3 Liquidation Buffer Check

```
# For a long position in isolated margin mode:
estimated_liq_price = entry × (1 − (1 / leverage) + maintenance_margin_rate)

if estimated_liq_price >= stop_price:
    # Stop-loss is beyond liquidation — dangerous
    # Either reduce leverage or reject the trade
```

The stop-loss must be reached before liquidation is triggered. If not, the trade is rejected or leverage is reduced until the buffer is safe.

### 15.4 Fee Sensitivity

At 10 USD equity, fees are proportionally large. Binance's commission formula:
```
commission = position_value × fee_rate
           = (quantity × entry_price × leverage) × fee_rate
```

With typical taker rate of 0.05%, on a 100 USD notional: fee = 0.05 USD (entry) + 0.05 USD (exit) = 0.10 USD. On a 10 USD account this is 1% of capital per round trip. Prefer **maker-style entries** (limit orders at retest levels) to pay 0.02% instead of 0.05%, saving 60% on entry fees.

---

## 16. Order Entry and Order Types

### 16.1 Preferred Order Form: Limit Retest Entry

The AQRR strategy favours **limit orders** placed at key structural levels (breakout retest, pullback zone, range boundary). This achieves:
- Maker fee (0.02% vs 0.05% taker) — critical savings on small account
- Better fill price than a market chase entry
- Natural expiry control (limit orders do not fill if price never returns)

### 16.2 Alternative Order Forms

| Setup Type | Preferred Order Type | Fallback |
|---|---|---|
| Breakout retest | Limit at broken level | Limit slightly above (long) / below (short) |
| Pullback entry | Limit at EMA zone / rejection candle low | Limit at last close of signal candle |
| Range bounce | Limit at range boundary | Limit at 0.2 × ATR inside boundary |
| Confirmed breakout (no retest expected) | Stop-limit order above/below key level | — |

### 16.3 Bracket Orders

Upon order fill, the bot immediately places:
- A **stop-market** (or stop-limit) order for the stop-loss price
- A **take-profit limit** order at the 3R target price

These must be placed atomically or as quickly as possible after fill confirmation to prevent unprotected exposure.

---

## 17. Pending Order Expiry Logic

Every pending (unfilled) order has an explicit validity window. If not filled within this window, the order is automatically cancelled.

### 17.1 Expiry Rules

| Entry Type | Expiry Window (Default) | Rationale |
|---|---|---|
| Breakout retest limit | 3–6 signal candles = 45–90 minutes (15m candles) | Retest typically happens quickly or not at all |
| Pullback limit | 4–8 signal candles = 60–120 minutes | Pullbacks can take slightly longer |
| Range bounce limit | 2–4 signal candles = 30–60 minutes | Bounces from extremes are typically sharp |
| Stop entry (breakout) | 2–3 signal candles = 30–45 minutes | Momentum-based; stale if not triggered quickly |

### 17.2 Staleness Detection

In addition to time-based expiry, a pending order should be cancelled if:
- The price has moved away from the entry zone by more than `2 × ATR(14, 15m)` without filling
- The regime has shifted since the order was placed (e.g., trend regime collapses into range while a breakout order is waiting)
- A market condition filter trigger fires on that symbol (extreme volatility, spread spike)

### 17.3 Cancellation Endpoint

```
DELETE /fapi/v1/order
  symbol = [symbol]
  orderId = [pending_order_id]
```

Log every cancellation with reason.

---

## 18. Open Position Lifecycle Management

Once a position is open, the bot manages the full lifecycle automatically.

### 18.1 Active Monitoring

Every scan cycle, the bot checks all open positions for:
- Current mark price vs stop-loss price
- Current mark price vs take-profit price
- Whether original trade logic remains structurally valid
- Whether an emergency condition (Section 22) requires forced closure

### 18.2 No Forced Time Limit

Open positions are **not closed by a fixed timer**. A trade that is working correctly should be allowed to run until it reaches the stop, the take-profit, or a management decision is triggered.

### 18.3 Position Monitoring Endpoint

```
GET /fapi/v2/positionRisk
```

Used to check current unrealised PnL, mark price, and position size for all active positions.

---

## 19. Stop-Loss Management

### 19.1 Initial Stop-Loss

Placed immediately after fill confirmation. The stop price is the structural level computed during trade plan construction (Section 9).

Order type: `STOP_MARKET` (preferred) or `STOP` on Binance Futures.

### 19.2 Stop Modification: Break-Even Management

Once the position moves in the profitable direction by **1R** (the full initial risk distance), the stop-loss may optionally be moved to break-even (entry price):

```
if current_mark_price ≥ entry + 1 × r_distance (long):
    if current_stop < entry:
        move stop to entry  # risk-free trade
```

This is optional but recommended for small accounts where capital preservation is paramount.

### 19.3 No Premature Widening

Stop-loss must **never** be widened after entry. If the price approaches the stop without being invalidated by structure, the stop holds. Widening stops to avoid losses violates the integrity of the 1:3 R:R structure.

---

## 20. Take-Profit Management

### 20.1 Initial Take-Profit: Hard 3R Target

The take-profit is placed at exactly the 3R level immediately after fill.

Order type: `TAKE_PROFIT_MARKET` or `LIMIT` on Binance Futures.

### 20.2 Optional: Extended Profit Capture After 3R

If the trade reaches the 3R target, one of the following approaches may be used:

**Option A — Full close at 3R (conservative, default for small account):**
Close the entire position at 3R. Simple, consistent, maximises R realisation rate.

**Option B — Partial close at 3R + trailing stop on remainder:**
Close 50–75% at 3R, then trail the stop on the remainder to capture extended moves.
- The trailing stop should follow the 15m swing structure
- Minimum acceptance: residual position must still risk a small amount of originally-captured profit

> For a 10 USD account, Option A is strongly recommended due to fee sensitivity and the difficulty of managing fractional position sizes efficiently.

### 20.3 R:R Integrity Rule

No management decision should **systematically** cause realised R to fall below 3.0 on winning trades. If a management rule is added that exits early, it must be evaluated for its effect on average realised R across the full trade sample.

---

## 21. Re-entry Policy

A coin that has been traded before is **not permanently blocked** from future signals.

### 21.1 Re-entry Conditions

A re-entry on the same symbol is allowed if:
- The previous position on that symbol is fully closed (not still open)
- The new signal is generated from a **fresh setup** — a structurally different price level with new confirmation
- The new setup passes all quality scoring requirements independently
- The symbol is not on a temporary cooldown (see below)

### 21.2 Cooldown After Loss

After a stop-loss is hit on a symbol, apply a **cooldown period** before re-entering:

```
cooldown_after_loss = 4 signal candles = 60 minutes (starter default)
```

During cooldown, the symbol is excluded from candidate generation. This prevents revenge-trading into the same adverse move.

### 21.3 Cooldown After Profit

No mandatory cooldown after a profitable trade. If a fresh high-quality setup forms immediately after a winning trade on the same symbol, the bot may re-enter.

---

## 22. Risk Controls and Circuit Breakers

### 22.1 Daily Loss Limit

```
if unrealised_losses + realised_losses_today > equity × 0.05:   # 5% of equity
    suspend all new trade entries for the remainder of the calendar day
    maintain existing open positions normally
```

### 22.2 Maximum Drawdown Kill Switch

```
if current_equity < initial_equity × 0.70:   # 30% drawdown from start
    close all open positions immediately
    cancel all pending orders
    suspend all trading
    alert user
```

These are proposed defaults. The values must be set and confirmed before live deployment. They are not specified in the original brief and represent essential protective parameters.

### 22.3 Consecutive Loss Circuit Breaker

```
if consecutive_stopped_out_trades >= 3:
    pause new entries for 24 hours
    log event and notify user
```

### 22.4 Emergency Position Close Conditions

A position should be closed immediately (regardless of SL/TP status) if:
- Liquidation price is approached within a configurable buffer (e.g., mark price within 5% of liquidation price)
- An exchange-level anomaly is detected (e.g., API returns mark price discrepancy > 10% vs last known price)

---

## 23. Exchange Reality Constraints

### 23.1 Minimum Notional

Binance USDⓈ-M futures historically enforce a minimum order notional of approximately **1 USD** per order. This constraint limits what is tradeable at 10 USD equity. Always check via `exchangeInfo → filters → MIN_NOTIONAL`.

With 10 USD equity and 1% risk per trade across 3 trades:
- Per-trade risk = 0.033 USD
- With 10× leverage → position notional = 0.33 USD (below 1 USD minimum)
- With 30× leverage → position notional = 1 USD (barely meets minimum)

This means the effective leverage required to meet minimum notional on some symbols may be higher than comfortable. The strategy should avoid symbols where this forces unsafe leverage.

### 23.2 Fee Impact on Small Account

| Scenario | Fee Cost | % of 10 USD Equity |
|---|---|---|
| 1 round trip, 100 USD notional, taker (0.05%) | 0.10 USD | 1.0% |
| 1 round trip, 100 USD notional, maker (0.02%) | 0.04 USD | 0.4% |
| 3 trades open/close, maker, 30 USD notional each | 0.036 USD | 0.36% |

Maker preference is not optional on a 10 USD account — it is a **requirement** for long-term viability.

### 23.3 Tick Size and Lot Size Compliance

Every order price must be a valid multiple of `tickSize` (from `PRICE_FILTER`).
Every order quantity must be a valid multiple of `stepSize` (from `LOT_SIZE`).

```python
price = round(price / tick_size) * tick_size
quantity = floor(quantity / step_size) * step_size
```

### 23.4 Isolated Margin Mode

Use **isolated margin** mode per position (not cross margin). This ensures that a liquidation event on one position does not affect the capital allocated to other positions.

Set before placing any order:
```
POST /fapi/v1/marginType
  symbol = [symbol]
  marginType = ISOLATED
```

---

## 24. Full Pseudocode — Scan and Execution Engine

```pseudo
INPUTS:
  equity_usd = current account equity (from GET /fapi/v2/account)
  max_positions = 3
  min_rr = 3.0
  risk_pct_per_trade = 0.01          # 1% per trade (starter)
  quality_threshold = 70             # minimum score to pass
  correlation_limit = 0.70           # max inter-trade correlation
  timeframes = {signal: 15m, context: 1h, higher: 4h}
  scan_interval = 60                 # seconds

MAIN LOOP (runs every scan_interval seconds):

  # ── Step 0: Check account state ────────────────────────────────────────
  open_positions = GET /fapi/v2/positionRisk (filter: positionAmt != 0)
  pending_orders = GET /fapi/v1/openOrders
  available_slots = max_positions - len(open_positions)

  check_circuit_breakers(equity_usd, open_positions)   # halt if triggered

  if available_slots == 0:
    SLEEP(scan_interval); CONTINUE

  # ── Step 1: Load universe ──────────────────────────────────────────────
  exchange_info = GET /fapi/v1/exchangeInfo
  universe = [s for s in exchange_info.symbols if s.status == "TRADING"]

  # ── Step 2: Symbol-level filters ──────────────────────────────────────
  filtered_universe = []
  FOR symbol IN universe:
    if fails_liquidity_filter(symbol):  SKIP
    if fails_spread_filter(symbol):     SKIP
    filtered_universe.append(symbol)

  # ── Step 3–7: Feature computation and candidate generation ─────────────
  candidates = []
  FOR symbol IN filtered_universe:
    klines_15m = GET /fapi/v1/klines (symbol, interval=15m, limit=200)
    klines_1h  = GET /fapi/v1/klines (symbol, interval=1h,  limit=200)
    klines_4h  = GET /fapi/v1/klines (symbol, interval=4h,  limit=100)

    features = compute_features(klines_15m, klines_1h, klines_4h)

    if fails_volatility_shock_filter(features):  SKIP
    if fails_spread_anomaly_filter(features):    SKIP
    if fails_funding_sanity_filter(symbol):      SKIP

    regime = classify_regime(features.adx_1h)

    if regime == TREND or regime == UNCERTAIN:
      cand_list = generate_trend_candidates(symbol, features, min_rr, regime)
    elif regime == RANGE:
      cand_list = generate_range_candidates(symbol, features, min_rr)

    FOR cand IN cand_list:
      cand.score = compute_quality_score(cand, features)
      if cand.score >= quality_threshold:
        candidates.append(cand)

  # ── Step 8: Cross-symbol ranking and correlation filter ────────────────
  ranked = sorted(candidates, by=score, descending=True)

  selected = []
  FOR cand IN ranked:
    if len(selected) == available_slots: BREAK
    corr_ok = all(
      abs(pearson_corr(cand.symbol, s.symbol, last_72_1h_bars)) < correlation_limit
      for s in selected
    )
    if corr_ok:
      selected.append(cand)

  # ── Step 9: Decision gate ──────────────────────────────────────────────
  if len(selected) == 0:
    LOG("No qualifying setups this cycle.")
    SLEEP(scan_interval); CONTINUE

  # ── Step 10: Sizing and execution ──────────────────────────────────────
  total_risk_usd = equity_usd × risk_pct_per_trade
  per_trade_risk = total_risk_usd / len(selected)

  FOR cand IN selected:
    r_dist = abs(cand.entry_price - cand.stop_price)
    qty_raw = per_trade_risk / r_dist
    leverage = choose_leverage(cand.symbol, qty_raw, equity_usd, r_dist)
    qty = floor(qty_raw / step_size(cand.symbol)) * step_size(cand.symbol)

    if qty * cand.entry_price < min_notional(cand.symbol):
      LOG("Trade rejected: below min notional after sizing.")
      CONTINUE

    set_isolated_margin(cand.symbol)
    set_leverage(cand.symbol, leverage)

    place_entry_limit_order(cand.symbol, cand.direction, qty, cand.entry_price)
    schedule_order_expiry(cand, expiry_candles=4)

    # After fill confirmed:
    place_stop_loss_order(cand.symbol, cand.direction, qty, cand.stop_price)
    place_take_profit_order(cand.symbol, cand.direction, qty, cand.tp_price)

  SLEEP(scan_interval)

# ── Expiry management (runs in parallel) ──────────────────────────────────
EXPIRY LOOP (runs every 60 seconds):
  FOR order IN pending_orders:
    if order.age_in_candles >= order.expiry_limit:
      CANCEL order via DELETE /fapi/v1/order
      LOG("Order cancelled: expired.")
    if market_condition_changed(order.symbol):
      CANCEL order
      LOG("Order cancelled: market condition change.")
```

---

## 25. Parameter Reference Table

| Parameter | Starter Default | Tuning Direction | Notes |
|---|---|---|---|
| Scan interval | 60 seconds | Longer = less load | Balance between reactivity and API cost |
| Signal TF | 15m | Shorter = more signals (noisier) | Main entry timeframe |
| Context TF | 1h | — | Regime and trend context |
| Higher TF | 4h | — | Optional macro filter |
| Quality threshold | 70 / 100 | Raise if trading too much; lower if never trading | Core selectivity gate |
| Max positions | 3 | Hard requirement | Never exceed |
| Risk per trade | 1.0% of equity | Lower if account shrinks | Per-trade risk budget |
| Correlation limit | 0.70 | 0.65–0.75 range | Inter-trade diversification |
| ADX trend threshold | 25 | 20–30 range | Regime classification |
| ADX range threshold | 15 | 12–18 range | Regime classification |
| Order expiry | 4 signal candles | 3–8 range | Freshness window |
| Volatility shock percentile | 90th | 85–95 range | Filter extreme regimes |
| Spread anomaly multiplier | 3× median | 2–4× range | Filter poor execution |
| Funding rate threshold | 0.15% | 0.10–0.20% range | Avoid adverse carry |
| Max leverage cap | 10× | Lower for safety | Hard ceiling on auto-selected leverage |
| Break-even move trigger | 1R gain | — | Optional, conservative |
| Cooldown after loss | 4 candles (60 min) | 2–8 candles | Revenge-trade prevention |
| Daily loss limit | 5% of equity | 3–7% range | Per-day circuit breaker |
| Max drawdown kill | 30% of equity | 25–40% range | Hard protection level |
| Consecutive loss pause | 3 trades | — | Trigger for 24h pause |
| Min 24h volume (USD) | 10,000,000 | Adjust per risk appetite | Liquidity filter |
| Max relative spread | 0.10% | 0.07–0.15% range | Execution quality filter |

---

## 26. Backtest Design Requirements

A backtest that does not model Binance USDⓈ-M specifics will produce misleading results.

### 26.1 Required Data Sources

| Data | Endpoint | Notes |
|---|---|---|
| OHLCV klines | GET /fapi/v1/klines | Primary price data |
| Exchange info (filters) | GET /fapi/v1/exchangeInfo | Tick size, lot size, notional |
| Funding rate history | GET /fapi/v1/fundingRate | For positions held across 8h intervals |
| Mark price history | GET /fapi/v1/markPriceKlines | Liquidation-accurate PnL |

### 26.2 Backtest Realism Checklist

- [ ] Model Binance maker fee (0.02%) for limit fills and taker fee (0.05%) for market/stop fills
- [ ] Model realistic slippage: at minimum `0.5 × tick_size` on limit entries; `1–2 × tick_size` on stop-market exits
- [ ] Enforce LOT_SIZE step rounding on every trade
- [ ] Enforce MIN_NOTIONAL on every trade
- [ ] Apply funding rate cost to positions held across funding timestamps (every 8h)
- [ ] Use mark price, not last price, for PnL and liquidation simulation
- [ ] No look-ahead bias: all signals must use only data available at the bar close
- [ ] Strict out-of-sample split: train on 70% of data, validate on remaining 30%
- [ ] Walk-forward evaluation: roll the window forward and re-run

### 26.3 Minimum Data Requirements

- At least 12 months of historical data
- Data spanning at least one bull phase, one bear phase, and one sideways / choppy phase
- At least 3 symbols backtested simultaneously (to test correlation filter behaviour)

---

## 27. Performance Metrics to Report

After backtest or live trading, report the following minimum set:

| Metric | Description |
|---|---|
| Total trades | Number of executed trades |
| Win rate | % of trades that reached TP before SL |
| Average R realised | Mean R multiple across all closed trades |
| Profit factor | Gross profit / gross loss |
| Expectancy per trade | Expected USD gain per trade |
| Maximum drawdown | Peak-to-trough equity drawdown (%) |
| Sharpe ratio | Risk-adjusted return (annualised) |
| Sortino ratio | Downside risk-adjusted return |
| Average holding time | Mean duration of closed trades |
| Maker fill rate | % of entries filled as maker (limit) vs taker |
| Total fees paid | Cumulative commission in USD |
| Funding costs | Cumulative funding fees in USD |
| Cancelled orders rate | % of pending orders cancelled before fill |
| Quality score distribution | Histogram of scores for all generated candidates |
| Regime distribution | % of trades from trend module vs range module |

**Break-even win rate note:** With a minimum 3R structure and ignoring costs, the break-even win rate is:
```
breakeven_win_rate = 1 / (1 + RR) = 1 / (1 + 3) = 25%
```
After fees, this threshold rises slightly. A post-cost win rate above ~28–30% is a reasonable starting target for this structure.

---

## 28. Implementation Checklist

### Data and Market Infrastructure
- [ ] Implement REST API polling for klines, exchange info, account, positions, orders
- [ ] Implement WebSocket subscription for real-time price updates (optional but improves reactivity)
- [ ] Cache exchange info; re-fetch every 1 hour
- [ ] Store spread samples for 7-day rolling median computation
- [ ] Store ATR% samples for 7-day percentile computation
- [ ] Store 1h return series per symbol for correlation computation

### Signal and Strategy Engine
- [ ] Feature computation module (EMA, ATR, ADX, RSI, volume ratio)
- [ ] Liquidity and market condition filter module
- [ ] Regime classifier
- [ ] Trend module (breakout + pullback candidate generators)
- [ ] Range module (mean reversion candidate generator with 3R gate)
- [ ] Trade plan constructor (entry / stop / TP with validation)
- [ ] Quality scoring engine
- [ ] Cross-symbol ranking
- [ ] Correlation computation and diversification filter
- [ ] Final selection logic with slot awareness

### Execution and Lifecycle
- [ ] Isolated margin mode setter per symbol
- [ ] Leverage setter per symbol
- [ ] Limit order placement (entries)
- [ ] Stop-market order placement (stop-loss)
- [ ] Take-profit order placement
- [ ] Order fill detection (WebSocket or polling)
- [ ] Expiry timer per pending order
- [ ] Staleness condition check for pending orders
- [ ] Order cancellation
- [ ] Break-even stop modification logic (optional)
- [ ] Position monitoring loop

### Risk Controls
- [ ] Daily loss accumulator and suspension logic
- [ ] Max drawdown kill switch
- [ ] Consecutive loss circuit breaker
- [ ] Liquidation proximity emergency close
- [ ] Account state check at every scan cycle

### Logging and Monitoring
- [ ] Log every candidate generated (symbol, score, regime, trade plan)
- [ ] Log every rejection with reason (filter failed, score below threshold, correlation blocked, not executable)
- [ ] Log every order placement, fill, cancellation, modification
- [ ] Log every circuit breaker trigger
- [ ] Equity curve tracking (record equity after every closed trade)

---

## 29. Known Limitations and Open Decisions

The following items are **not fully specified** in the original requirements brief and must be decided before finalising the live strategy:

| Open Item | Impact | Decision Needed |
|---|---|---|
| Preferred trading horizon | Determines timeframe selection, fee sensitivity, funding exposure | Confirm: 15m signal / 1h context (suggested) or change |
| Exact definition of "1:3 minimum R:R" | Does dynamic management (partial close, trailing) that exits before 3R violate the rule? | Confirm interpretation |
| Per-trade risk percentage | Directly drives position sizing; too high risks ruin on small account | Confirm: 1% (suggested) or adjust |
| Maximum leverage hard cap | Currently suggested at 10×; exchange allows up to symbol limits | Confirm cap |
| Margin mode | Isolated is recommended and assumed in this document | Confirm |
| Position mode | One-way vs hedge mode | Confirm; affects how SL/TP orders are structured |
| Universe exclusion list | Any specific coins to permanently exclude (e.g., tokens with regulatory risk)? | Confirm or leave dynamic |
| Maker vs taker fee rates | Actual rates depend on VIP level and BNB discount | Confirm applicable rates |
| Walk-forward validation protocol | How frequently to re-evaluate parameters in live operation? | Define schedule |

---

*Document version: 1.0 — Generated 2026-04-08*  
*Strategy family: Adaptive Quality-Ranked Regime Strategy (AQRR)*  
*Target venue: Binance USDⓈ-M Perpetual Futures*  
*Account size basis: 10 USD*
