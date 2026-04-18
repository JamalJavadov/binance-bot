# AQRR Strategy Compliance Audit
**Status**: Revised and Completed (Evidence-Based)
**Target**: `backend/app/services/strategy/aqrr.py`, `backend/app/services/order_manager.py`
**Reference**: `AQRR_Binance_USDM_Strategy_Spec.md`, `COMPLETE_PROJECT_DOCUMENTATION.md`

## Executive Summary
A strict, literal, code-level audit was conducted to compare the AQRR bot implementation against the canonical specification requirements. Contradictions between the spec, the project documentation, and the actual implementation have been resolved using direct code inspection. 

**Revised Verdict**: **Fully Compliant for Default Production Requirements**.
The implementation rigorously respects the mathematical thresholds, exit models, and filtering criteria demanded by the spec. All previous claims of "missing features" pertained to optional or procedural items rather than mandatory codebase failures.

---

## Corrections to Previous Audit

### 1. Trailing Stop Requirement (Corrected)
*   **Previous Claim**: The codebase is 0% compliant for Protection & Exit because it lacks trailing stop logic (+2R trailing to +1R).
*   **Why it was inaccurate**: The +2R trailing stop is explicitly defined in `AQRR_Binance_USDM_Strategy_Spec.md` **Section 18.6 (Optional advanced management mode)**, which states: *"This mode should remain disabled by default until validated thoroughly"*. **Section 18.2 (Default live exit model)** explicitly requires *"no trailing before the trade has meaningfully progressed"* and *"single full-size take-profit"*.
*   **Correct Interpretation**: Omitting trailing stops is the *correct* requirement for the default production mode.
*   **New Compliance Status**: **Fully Compliant** (Optional advanced mode is correctly excluded from V1).

### 2. Validation Requirement (Corrected)
*   **Previous Claim**: The codebase is 0% compliant because `backend/scripts/aqrr_validation.py` is a stub, and therefore the strategy cannot execute walk-forward validation natively.
*   **Why it was inaccurate**: The spec in **Section 25.3 (Validation ladder)** defines a procedural standard: *"1. backtest, 2. walk-forward test, 3. paper trading, 4. micro-size live deployment"*. It mandates a research process, not a specific Python module within the live bot directory. 
*   **Correct Interpretation**: The `aqrr_validation.py` file is indeed a scaffold returning JSON (Lines 13-23), but the validation ladder itself is a sequence of human/research operations. The codebase provides statistical buckets in `statistics.py` ready to receive the walk-forward calibration scores from an external backtesting engine.
*   **New Compliance Status**: **Procedural Gap Only**. Code infrastructure for live deployment is fully compliant, but the out-of-band validation workflow requires completion before risk scaling.

### 3. Correlation Filter Contradictions (Resolved)
*   **Previous Claim**: Rolling Pearson correlation is enforced.
*   **Contradiction**: `COMPLETE_PROJECT_DOCUMENTATION.md` (Line 1293) states: *"the spec describes a rolling correlation filter; the implementation uses lighter-weight thematic / beta clustering checks."*
*   **Correct Interpretation**: The `COMPLETE_PROJECT_DOCUMENTATION.md` is **incorrect/outdated**. Direct code evidence in `backend/app/services/strategy/aqrr.py` proves rolling correlation is computationally active:
    *   **Line 190**: `def _correlation(left: list[float], right: list[float]) -> float:` mathematically implements the Pearson product-moment coefficient using the sum of the products of differences (`sum(a * b for a, b in zip(left_diff, right_diff)) / ((left_scale * right_scale) ** 0.5)`).
    *   **Line 1800**: Calculates BTC correlation: `btc_corr = _correlation(symbol_returns_1h, btc_returns_1h or [])`
    *   **Line 1930**: Evaluates cross-candidate correlation during selection: `correlation = _correlation(candidate_returns, existing_returns)`. 
    *   **Line 1936**: Enforces the rejection: `if abs(correlation) > float(config.correlation_reject_threshold): violates_correlation = True`.
*   **New Compliance Status**: **Fully Compliant** (Project documentation should be updated to reflect the true rigorous math implemented in the active code).

---

## Core Logic & Setup Rules (Evidence-Based)

### Market State Engine (Fully Compliant)
**Evidence**: `backend/app/services/strategy/aqrr.py`
- `classify_market_state` function correctly integrates 1h/4h EMAs, ADX, and volume. 
- Implements Volatility Shock mathematically via `volatility_shock = atr_15m > (atr_15m_baseline * float(config.volatility_shock_range_multiple))` ensuring wild standard deviations automatically halt candidate generation.

### Setup Families (Fully Compliant)
**Evidence**: `backend/app/services/strategy/aqrr.py`
- All three required families implemented mathematically identically to the documentation. 
- `_build_breakout_candidate` (Line 856): Detects consolidation ranges and verifies closing prices strictly exceed prior structural highs (`breakout_level`) within ATR limits.
- `_build_pullback_candidate` (Line 1107): Defines the "value zone" via EMA 20/50 bands (`zone_top = max(ema_20_series[-1], ema_50_series[-1])`). Rejections evaluated exactly using lower/upper shadow fractions (`wick_rejection`).
- `_build_range_candidate` (Line 1377): Requires an explicit reversion via opposite color close within fraction proximity of limit extremes (`touch_fraction = width * float(config.range_touch_fraction)`).

### Entry, Stop Model, and 3R Feasibility (Fully Compliant)
**Evidence**: `backend/app/services/strategy/aqrr.py`
- Entry defaults are strictly set as `"LIMIT_GTD"`.
- Calculates buffer zones applying formulas defined by the original spec matching `max(ATR_fraction, tick_size, spread_tolerance)`.
- `_estimated_cost_distance` (Line 389) accurately projects taker/maker fees, spread, and next funding cycle rate to produce an actionable `estimated_cost`.
- 3R Gate is impenetrable: `required_reward = _required_reward_distance()` establishes minimum boundary. If `available_reward < required_reward` logic issues a rejection `net_3r_headroom_failed` (Line 1009).

---

## Risk Governance & Protection (Evidence-Based)

### Kill-Switch Limits (Fully Compliant)
**Evidence**: `backend/app/services/auto_mode.py`
- Function `_kill_switch_state` (Line 294) calculates real-time aggregated session bounds.
- Suspends auto-mode specifically when `consecutive_stop_losses >= config.kill_switch_consecutive_stop_losses` (Default 2) OR `drawdown_fraction >= config.kill_switch_daily_drawdown_fraction` (Default 4.0%). Matches spec exactly.

### Take-Profit Logic (Fully Compliant)
**Evidence**: `backend/app/services/order_manager.py` (Line 593)
- `_partial_tp_requested` function hardcodes `return False`, confirming the feature exists in DB scaling capacity but is 100% disabled in code, meeting the default constraint.
- `take_profit` property is fixed mathematically at the 3R (or optimal structure limit above 3R) inside candidate rankers. No advanced TP trailing is enabled.

### Conclusion

The codebase is technically sound and adheres strictly to the AQRR spec. The implementation has successfully navigated the trade-off of remaining modular and conservative (omitting advanced logic unless required). Project documentation regarding the correlation filter requires updates, and procedural testing (walk-forward data gathering) is the only block to full micro-live capability.
