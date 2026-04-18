# AQRR Conformance and Validation Plan

## 1. Purpose
This document maps every existing and planned test to the canonical AQRR Strategy Specification (`AQRR_Binance_USDM_Strategy_Spec.md`), records the Phase 2 offline backtest runner architecture, and tracks code quality findings.

---

## 2. Test-to-Spec Mapping

Each row maps a test (or test category) to the spec section it validates.

| Test File | Test Function | Spec Section | Requirement |
|---|---|---|---|
| `test_strategy.py` | `test_classify_market_state_detects_bull_trend` | §7.1 | Bull: EMA50 > EMA200 (1h+4h), ADX ≥ 22, positive slope |
| `test_strategy.py` | `test_classify_market_state_detects_balanced_range` | §7.3 | Range: ADX ≤ 18, BB compressed, mean-reversion crosses |
| `test_strategy.py` | `test_classify_market_state_requires_recent_range_containment` | §7.4 | Unstable: range detection requires 1h containment proof |
| `test_strategy.py` | `test_volatility_shock_uses_exact_30_day_atr_percentile_window` | §7.5 | Volatility shock: ATR > 2.5× percentile baseline |
| `test_strategy.py` | `test_evaluate_symbol_returns_unstable_no_trade` | §20.3 | No-trade if market state is unstable |
| `test_strategy.py` | `test_evaluate_symbol_routes_bull_candidates_and_sorts_by_rank` | §9.6 | Candidates sorted descending by rank_value |
| `test_strategy.py` | `test_evaluate_symbol_returns_raw_builder_rejection_reasons` | §26.3 | Decision logs must record rejection reasons |
| `test_strategy.py` | `test_evaluate_symbol_returns_required_leverage_rejection_reason` | §20.5 | No-trade if required leverage exceeds allowed level |
| `test_strategy_diagnostics.py` | `test_build_breakout_candidate_constructs_stop_entry_setup` | §9.3 | Breakout: momentum candle → STOP_ENTRY; 3R gate enforced |
| `test_strategy_diagnostics.py` | `test_build_breakout_candidate_uses_retest_zone_for_limit_entry` | §9.3 | Breakout retest: price returns to level → LIMIT_GTD |
| `test_strategy_diagnostics.py` | `test_build_pullback_candidate_constructs_trend_continuation_setup` | §9.3 | Pullback: EMA zone touch + rejection evidence required |
| `test_strategy_diagnostics.py` | `test_build_pullback_candidate_accepts_bullish_engulf_reclaim_evidence` | §9.3 | Pullback: engulf/reclaim counted as rejection evidence |
| `test_strategy_diagnostics.py` | `test_build_pullback_candidate_accepts_short_local_lower_high_evidence` | §9.3 | Pullback SHORT: local structure recovery required |
| `test_strategy_diagnostics.py` | `test_build_pullback_candidate_accepts_countertrend_momentum_loss_evidence` | §9.3 | Pullback: momentum loss counted as rejection evidence |
| `test_strategy_diagnostics.py` | `test_build_range_candidate_constructs_reversion_setup` | §9.3 | Range: support touch + reversion signal → LIMIT_GTD |
| `test_strategy_diagnostics.py` | `test_evaluate_symbol_applies_net_three_r_hard_gate` | §9.4 / §18.4 | All setups must achieve net R ≥ 3.0 after costs |
| `test_strategy_diagnostics.py` | `test_estimated_cost_prefers_limit_entries_and_only_applies_relevant_funding` | §21.4 | Fees are part of risk; limit entries cheaper than stop entries |
| `test_strategy_diagnostics.py` | `test_account_specific_commission_can_flip_net_three_r_acceptance` | §21.4 | High fees can invalidate otherwise-viable setup |
| `test_strategy_diagnostics.py` | `test_estimated_cost_uses_adverse_funding_history_when_next_funding_is_unknown` | §21.4 | Funding cost must be included in pre-trade risk calculation |
| `test_strategy_diagnostics.py` | `test_evaluate_symbol_rejects_tier_c_execution` | §20.4 | No-trade if spread is unacceptable |
| `test_strategy_diagnostics.py` | `test_required_leverage_uses_remaining_slot_and_risk_budget` | §23 (defaults) | Leverage adjusted per remaining slots |
| `test_strategy_diagnostics.py` | `test_select_candidates_rejects_correlation_conflict` | §23 / Correlation filter | Pearson r > 0.80 same direction → rejected |
| `test_strategy_diagnostics.py` | `test_select_candidates_allows_negative_correlation_same_direction` | Correlation filter | Negative correlation same direction → allowed |
| `test_strategy_diagnostics.py` | `test_select_candidates_allows_high_correlation_when_direction_differs` | Correlation filter | High correlation opposite directions → allowed |
| `test_strategy_diagnostics.py` | `test_select_candidates_rejects_cluster_conflict` | §12 (diversification) | Max 2 candidates from same thematic cluster |
| `test_strategy_diagnostics.py` | `test_select_candidates_rejects_btc_beta_conflict` | §12 (diversification) | Max 2 high-BTC-beta candidates same direction |
| `test_strategy_diagnostics.py` | `test_select_candidates_tracks_btc_beta_by_direction` | §12 (diversification) | BTC beta limit is direction-aware |
| `test_strategy_diagnostics.py` | `test_classify_market_state_flags_pump_dump_profile_as_unstable` | §7.4 | Pump/dump price profile → unstable |
| `test_strategy_diagnostics.py` | `test_classify_market_state_flags_severe_spread_and_liquidity_degradation_as_unstable` | §7.4 / §20.4 | Severe spread + low liquidity → unstable |
| `test_conformance_aqrr.py` *(new)* | `test_score_threshold_tier_a_minimum_is_70` | §9.5 | Tier A threshold: score < 70 → rejected |
| `test_conformance_aqrr.py` *(new)* | `test_score_threshold_tier_b_minimum_is_78` | §9.5 | Tier B threshold: score < 78 → rejected |
| `test_conformance_aqrr.py` *(new)* | `test_no_trade_zero_slots` | §20.8 | No-trade if 3 entry slots already occupied |
| `test_conformance_aqrr.py` *(new)* | `test_no_trade_correlation_conflict_blocks_all_candidates` | §20.7 | No new trade if all candidates create excess correlation |
| `test_conformance_aqrr.py` *(new)* | `test_default_exit_model_is_single_tp_single_sl` | §18.2 | Default: no partial TP, no trailing |
| `test_conformance_aqrr.py` *(new)* | `test_kill_switch_two_consecutive_stop_losses` | §21.1 | Kill switch: 2 consecutive losses → suspend entries |
| `test_conformance_aqrr.py` *(new)* | `test_kill_switch_four_pct_drawdown` | §21.1 | Kill switch: 4% drawdown → suspend entries |
| `test_conformance_aqrr.py` *(new)* | `test_max_aggregate_open_risk_six_pct` | §21.2 | Max combined worst-case SL exposure: 6% of equity |
| `test_conformance_aqrr.py` *(new)* | `test_bear_regime_generates_short_candidates_only` | §7.2 | Bear: only short setups allowed |
| `test_conformance_aqrr.py` *(new)* | `test_range_regime_generates_range_candidates_only` | §7.3 | Range: only range-reversion setups allowed |
| `test_conformance_aqrr.py` *(new)* | `test_select_candidates_max_three_simultaneous` | §20.8 / §23 | Max 3 pending entry orders at any time |
| `test_conformance_aqrr.py` *(new)* | `test_breakout_entry_default_is_limit_gtd` | §17 / §9.3 | Default entry type: LIMIT_GTD where applicable |
| `test_conformance_aqrr.py` *(new)* | `test_correlation_reject_threshold_is_0_80` | §23 (defaults table) | Correlation reject threshold: abs(r) > 0.80 |

---

## 3. Coverage Summary

| Spec Area | Status |
|---|---|
| Market state classification (§7) | ✅ Covered (5 tests) |
| Setup families — Breakout (§9.3) | ✅ Covered (4 tests) |
| Setup families — Pullback (§9.3) | ✅ Covered (5 tests) |
| Setup families — Range (§9.3) | ✅ Covered (1 test) |
| Net 3R feasibility gate (§9.4) | ✅ Covered (3 tests) |
| Score thresholds (§9.5) | ⚠️ Gap — added in test_conformance_aqrr.py |
| Candidate selection / ranking (§9.6) | ✅ Covered (7 tests) |
| Correlation rejection (§23) | ✅ Covered (5 tests) |
| No-trade conditions (§20) | ⚠️ Partial — expanded in test_conformance_aqrr.py |
| Risk governance / kill switch (§21) | ⚠️ Gap — added in test_conformance_aqrr.py |
| Default exit model (§18.2) | ⚠️ Gap — added in test_conformance_aqrr.py |

---

## 4. Phase 2: Offline Backtest Runner Architecture

The offline runner (`backend/scripts/backtest_runner.py`) consumes pre-downloaded historical candles and feeds the real `evaluate_symbol()` function, producing structured candidate artifacts without touching any live execution.

### Data Flow
```
historical_fetch.py → CSV files (OHLCV)
    ↓
backtest_runner.py
    ├── load_candles(symbol, interval, path)
    ├── for each 15m timestamp window:
    │     evaluate_symbol(candles_15m[-N:], candles_1h[-N:], candles_4h[-N:], ...)
    │     → SymbolEvaluation (candidate or no-setup)
    │     record result + rejection reasons
    └── export BacktestRunArtifact (JSON)
```

### Boundaries
- **Offline only**: no DB, no gateway, no credentials
- **Pure Python**: all inputs are pre-loaded lists of `Candle` objects
- **Not a full simulator**: fills, PnL, and position tracking are **not** implemented in this phase

---

## 5. Code Quality Findings

| File | Issue | Severity | Notes |
|---|---|---|---|
| `order_manager.py` | 5,972 lines — significantly oversized | Medium | Candidate for decomposition into `entry_manager`, `protection_manager`, `sync_manager` |
| `auto_mode.py` | 2,218 lines — approaching threshold | Low | Core cycle and kill-switch logic could be separated |
| `aqrr.py` | 79,009 bytes — large but cohesive | Low | Well-structured; internal function boundaries are clear |
| `backend/scripts/aqrr_validation.py` | Scaffold stub only — misleadingly present | Medium | Should be replaced or clearly marked as placeholder |
| `test_strategy_diagnostics.py` | No spec citations in test names or docstrings | Low | Tests exist but linkage to spec is implicit |
| `statistics.py` | `import_walk_forward_buckets` uses string concatenation for bucket key | Low | Should use `build_candidate_stats_bucket()` helper instead |
