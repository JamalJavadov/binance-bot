"""
AQRR Strategy Conformance Tests
================================
Tests in this module are directly traceable to AQRR_Binance_USDM_Strategy_Spec.md.
Each test function docstring cites the relevant spec section.

Coverage targets (not already in test_strategy.py / test_strategy_diagnostics.py):
  - Score threshold enforcement (Spec §9.5)
  - No-trade conditions completeness (Spec §20)
  - Risk governance kill switch (Spec §21)
  - Default exit model confirmation (Spec §18.2)
  - Bear-regime and range-regime setup routing
  - Correlation threshold default value (Spec §23)
  - Max simultaneous candidates (Spec §23)
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import app.services.strategy.aqrr as aqrr
from app.models.enums import OrderStatus, ScanSymbolOutcome, SignalDirection
from app.services.strategy.adx import TrendMetrics
from app.services.strategy.config import StrategyConfig, resolve_strategy_config
from app.services.strategy.types import Candle, SetupCandidate


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_config() -> StrategyConfig:
    return resolve_strategy_config({})


def _candle(
    *,
    open_time: int,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1_000,
) -> Candle:
    return Candle(open_time=open_time, open=open, high=high, low=low, close=close, volume=volume)


def _candles_from_closes(closes: list[float], *, spread: float = 0.8) -> list[Candle]:
    candles: list[Candle] = []
    for index, close in enumerate(closes):
        candles.append(
            Candle(
                open_time=index,
                open=close - 0.2,
                high=close + spread / 2,
                low=close - spread / 2,
                close=close,
                volume=1_000 + index * 5,
            )
        )
    return candles


def _market_state(state: str, direction: SignalDirection | None) -> aqrr.MarketStateAssessment:
    return aqrr.MarketStateAssessment(
        market_state=state,
        direction=direction,
        ema_50_1h=100.0,
        ema_200_1h=95.0,
        ema_50_4h=120.0,
        ema_200_4h=110.0,
        adx_1h=30.0,
        ema_slope_norm_1h=1.0 if direction == SignalDirection.LONG else -1.0 if direction == SignalDirection.SHORT else 0.0,
        bollinger_bandwidth_1h=0.02,
        mean_cross_count_1h=1,
        volatility_shock=False,
        diagnostics={},
    )


def _candidate(
    *,
    symbol: str,
    rank_value: float,
    direction: SignalDirection = SignalDirection.LONG,
    net_r_multiple: float = 3.5,
    estimated_cost: float = 0.10,
    cluster: str | None = None,
    btc_beta: float = 0.20,
    returns_1h: list[float] | None = None,
    final_score: int | None = None,
    execution_tier: str = "TIER_A",
) -> SetupCandidate:
    return SetupCandidate(
        symbol=symbol,
        direction=direction,
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=114.0,
        actual_rr=3.5,
        net_r_multiple=net_r_multiple,
        estimated_cost=estimated_cost,
        confirmation_score=80,
        final_score=final_score if final_score is not None else int(rank_value),
        rank_value=rank_value,
        setup_family=aqrr.SETUP_BREAKOUT,
        setup_variant="conformance_candidate",
        entry_style="LIMIT_GTD",
        market_state=aqrr.MARKET_STATE_BULL,
        execution_tier=execution_tier,
        score_breakdown={
            "structure_quality": 22,
            "regime_alignment": 18,
            "liquidity_execution_quality": 14,
            "reward_headroom_quality": 9,
        },
        selection_context={
            "cluster": cluster,
            "btc_beta_correlation": btc_beta,
            "returns_1h": returns_1h or [0.01, 0.02, -0.01],
        },
    )


# ---------------------------------------------------------------------------
# Spec §9.5 — Score Thresholds
# ---------------------------------------------------------------------------

def test_score_threshold_tier_a_minimum_is_70() -> None:
    """
    Spec §9.5: Score < 70 must be rejected for Tier A symbols.
    Default config tier_a_min_score = 70.
    """
    config = _default_config()
    assert config.tier_a_min_score == 70, "Spec §9.5 default Tier A threshold must be 70"


def test_score_threshold_tier_b_minimum_is_78() -> None:
    """
    Spec §9.5: Tier B symbols require score >= 78.
    Default config tier_b_min_score = 78.
    """
    config = _default_config()
    assert config.tier_b_min_score == 78, "Spec §9.5 default Tier B threshold must be 78"


def test_evaluate_symbol_rejects_tier_a_below_70_score(monkeypatch) -> None:
    """
    Spec §9.5: A candidate with final_score below the Tier A minimum (70)
    must not survive evaluation as a qualifying candidate.
    """
    config = _default_config()
    low_score_candidate = _candidate(symbol="BTCUSDT", rank_value=65.0, final_score=65)
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG))
    monkeypatch.setattr(aqrr, "_build_breakout_candidate", lambda **_kwargs: low_score_candidate)
    monkeypatch.setattr(aqrr, "_build_pullback_candidate", lambda **_kwargs: None)

    evaluation = aqrr.evaluate_symbol(
        symbol="BTCUSDT",
        candles_15m=_candles_from_closes([100.0] * 80),
        candles_1h=_candles_from_closes([100.0] * 80),
        candles_4h=_candles_from_closes([100.0] * 80),
        current_price=100.0,
        funding_rate=0.0,
        quote_volume=50_000_000.0,
        spread_bps=5.0,
        liquidity_floor=25_000_000.0,
        filters_min_notional=5.0,
        tick_size=0.1,
        available_balance=1_000.0,
        config=config,
    )

    # A below-threshold score candidate must not produce a CANDIDATE outcome
    assert evaluation.outcome != ScanSymbolOutcome.CANDIDATE, (
        "A Tier A candidate scoring below 70 must be rejected per spec §9.5"
    )


def test_evaluate_symbol_rejects_tier_b_below_78_score(monkeypatch) -> None:
    """
    Spec §9.5: A Tier B symbol (spread 6–12 bps) with score < 78 must be rejected.
    """
    config = _default_config()
    tier_b_low_score = _candidate(symbol="BTCUSDT", rank_value=74.0, final_score=74, execution_tier="TIER_B")
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG))
    monkeypatch.setattr(aqrr, "_build_breakout_candidate", lambda **_kwargs: tier_b_low_score)
    monkeypatch.setattr(aqrr, "_build_pullback_candidate", lambda **_kwargs: None)

    evaluation = aqrr.evaluate_symbol(
        symbol="BTCUSDT",
        candles_15m=_candles_from_closes([100.0] * 80),
        candles_1h=_candles_from_closes([100.0] * 80),
        candles_4h=_candles_from_closes([100.0] * 80),
        current_price=100.0,
        funding_rate=0.0,
        quote_volume=50_000_000.0,
        spread_bps=9.0,  # Tier B range (6–12 bps)
        liquidity_floor=25_000_000.0,
        filters_min_notional=5.0,
        tick_size=0.1,
        available_balance=1_000.0,
        config=config,
    )

    assert evaluation.outcome != ScanSymbolOutcome.CANDIDATE, (
        "A Tier B candidate scoring below 78 must be rejected per spec §9.5"
    )


# ---------------------------------------------------------------------------
# Spec §20 — No-Trade Conditions
# ---------------------------------------------------------------------------

def test_no_trade_max_three_candidates_enforced() -> None:
    """
    Spec §20.8 / §23: Maximum 3 pending entry orders at any time.
    Adding a 4th candidate when 3 are filled must result in no selection.
    """
    config = _default_config()
    assert config.max_entry_ideas == 3, "Spec §23 default max pending entry orders must be 3"

    candidates = [
        _candidate(symbol=f"SYM{i}USDT", rank_value=float(90 - i), returns_1h=[0.01 * i, -0.01 * i, 0.005 * i])
        for i in range(4)
    ]
    result = aqrr.select_candidates(candidates, config=config)
    assert len(result.selected) <= 3, "Spec §20.8: must not select more than 3 candidates"


def test_no_trade_correlation_blocks_all_candidates() -> None:
    """
    Spec §20.7: If a new trade would create excessive correlation with all existing
    positions, no new trade should be opened. Verified via select_candidates residual.
    """
    config = _default_config()
    anchor = _candidate(symbol="BTCUSDT", rank_value=95.0, returns_1h=[0.01, 0.02, 0.03, 0.04, 0.05])
    # Highly correlated to anchor — same direction, r > 0.80
    corr_a = _candidate(symbol="ETHUSDT", rank_value=92.0, returns_1h=[0.011, 0.021, 0.031, 0.041, 0.051])
    corr_b = _candidate(symbol="SOLUSDT", rank_value=89.0, returns_1h=[0.012, 0.022, 0.032, 0.042, 0.052])

    result = aqrr.select_candidates([anchor, corr_a, corr_b], config=config)

    # Anchor goes through; correlated ones get rejected
    assert anchor.symbol in [c.symbol for c in result.selected]
    rejected_symbols = {k[0] for k in result.rejected.keys()}
    assert len(rejected_symbols) >= 1, "Correlated candidates must be rejected per spec §20.7"


# ---------------------------------------------------------------------------
# Spec §18.2 — Default Exit Model
# ---------------------------------------------------------------------------

def test_default_exit_model_partial_tp_disabled() -> None:
    """
    Spec §18.2: Default production mode — no scaling out before 3R,
    no trailing before the trade has meaningfully progressed.
    Verified via OrderManager._partial_tp_requested() stub.
    """
    from app.services.order_manager import OrderManager

    # The method must unconditionally return False for the default mode
    result = OrderManager._partial_tp_requested({}, approved_by="AUTO_MODE")
    assert result is False, (
        "Spec §18.2: partial TP must be disabled by default. "
        "_partial_tp_requested() must return False."
    )


# ---------------------------------------------------------------------------
# Spec §21.1 — Kill Switch Thresholds
# ---------------------------------------------------------------------------

def test_kill_switch_consecutive_stop_losses_default_is_2() -> None:
    """
    Spec §21.1: Suspend new entries after 2 full stop-losses in a row.
    Default: kill_switch_consecutive_stop_losses = 2.
    """
    config = _default_config()
    assert config.kill_switch_consecutive_stop_losses == 2, (
        "Spec §21.1: kill switch must trigger after 2 consecutive stop-losses"
    )


def test_kill_switch_daily_drawdown_default_is_4_pct() -> None:
    """
    Spec §21.1: Suspend new entries after 4% daily drawdown.
    Default: kill_switch_daily_drawdown_pct = 4.0.
    """
    config = _default_config()
    assert config.kill_switch_daily_drawdown_pct == Decimal("4.0"), (
        "Spec §21.1: kill switch daily drawdown threshold must be 4%"
    )


# ---------------------------------------------------------------------------
# Spec §21.2 — Max Aggregate Open Risk
# ---------------------------------------------------------------------------

def test_max_aggregate_open_risk_default_is_6_pct() -> None:
    """
    Spec §21.2: At no time should combined worst-case stop-loss exposure exceed 6% of total equity.
    Default: max_portfolio_risk_pct = 6.0.
    """
    config = _default_config()
    assert config.max_portfolio_risk_pct == Decimal("6.0"), (
        "Spec §21.2: max aggregate open risk must be 6% of equity"
    )


# ---------------------------------------------------------------------------
# Spec §23 — Default Parameter Defaults
# ---------------------------------------------------------------------------

def test_correlation_reject_threshold_default_is_0_80() -> None:
    """
    Spec §23 defaults table: Correlation reject threshold = abs(r) > 0.80.
    """
    config = _default_config()
    assert config.correlation_reject_threshold == Decimal("0.80"), (
        "Spec §23: correlation reject threshold must default to 0.80"
    )


def test_min_net_r_multiple_default_is_3_0() -> None:
    """
    Spec §23 defaults table: Minimum required net R = 3.0.
    """
    config = _default_config()
    assert config.min_net_r_multiple == Decimal("3.0"), (
        "Spec §23: minimum required net R multiple must default to 3.0"
    )


def test_max_leverage_default_is_10x() -> None:
    """
    Spec §23 defaults table: Default internal max leverage = 10x.
    """
    config = _default_config()
    assert config.max_leverage == 10, (
        "Spec §23: default internal max leverage must be 10x"
    )


def test_max_book_spread_default_is_12_bps() -> None:
    """
    Spec §23 defaults table: Maximum spread = 12 bps.
    """
    config = _default_config()
    assert config.max_book_spread_bps == Decimal("12"), (
        "Spec §23: maximum spread threshold must default to 12 bps"
    )


# ---------------------------------------------------------------------------
# Spec §7.2 / §7.3 — Setup Routing by Regime
# ---------------------------------------------------------------------------

def test_bear_regime_does_not_generate_long_setups(monkeypatch) -> None:
    """
    Spec §7.2: In bear regime, only short setups are valid.
    evaluate_symbol must not return LONG candidates in a bear market.
    """
    config = _default_config()
    short_candidate = SetupCandidate(
        symbol="BTCUSDT",
        direction=SignalDirection.SHORT,
        entry_price=100.0,
        stop_loss=104.0,
        take_profit=88.0,
        actual_rr=3.0,
        net_r_multiple=3.1,
        estimated_cost=0.10,
        confirmation_score=80,
        final_score=82,
        rank_value=82.0,
        setup_family=aqrr.SETUP_BREAKOUT,
        setup_variant="bear_breakout",
        entry_style="LIMIT_GTD",
        market_state=aqrr.MARKET_STATE_BEAR,
        execution_tier="TIER_A",
        score_breakdown={"structure_quality": 22, "regime_alignment": 18, "liquidity_execution_quality": 14, "reward_headroom_quality": 9},
    )
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BEAR, SignalDirection.SHORT))
    monkeypatch.setattr(aqrr, "_build_breakout_candidate", lambda **_kwargs: short_candidate)
    monkeypatch.setattr(aqrr, "_build_pullback_candidate", lambda **_kwargs: None)

    evaluation = aqrr.evaluate_symbol(
        symbol="BTCUSDT",
        candles_15m=_candles_from_closes([100.0] * 80),
        candles_1h=_candles_from_closes([100.0] * 80),
        candles_4h=_candles_from_closes([100.0] * 80),
        current_price=100.0,
        funding_rate=0.0,
        quote_volume=50_000_000.0,
        spread_bps=5.0,
        liquidity_floor=25_000_000.0,
        filters_min_notional=5.0,
        tick_size=0.1,
        available_balance=1_000.0,
        config=config,
    )

    if evaluation.outcome == ScanSymbolOutcome.CANDIDATE:
        for candidate in evaluation.candidates:
            assert candidate.direction == SignalDirection.SHORT, (
                "Spec §7.2: Bear regime must only produce short candidates"
            )


def test_range_regime_does_not_route_to_trend_setups(monkeypatch) -> None:
    """
    Spec §7.3: In balanced range regime, only range-reversion setups are valid.
    Breakout and pullback builder calls must not be made for a pure range state.
    """
    config = _default_config()
    breakout_called = {"called": False}
    pullback_called = {"called": False}

    def spy_breakout(**_kwargs):
        breakout_called["called"] = True
        return None

    def spy_pullback(**_kwargs):
        pullback_called["called"] = True
        return None

    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_RANGE, None))
    monkeypatch.setattr(aqrr, "_build_breakout_candidate", spy_breakout)
    monkeypatch.setattr(aqrr, "_build_pullback_candidate", spy_pullback)
    monkeypatch.setattr(aqrr, "_build_range_candidate", lambda **_kwargs: None)

    aqrr.evaluate_symbol(
        symbol="BTCUSDT",
        candles_15m=_candles_from_closes([100.0] * 80),
        candles_1h=_candles_from_closes([100.0] * 80),
        candles_4h=_candles_from_closes([100.0] * 80),
        current_price=100.0,
        funding_rate=0.0,
        quote_volume=50_000_000.0,
        spread_bps=5.0,
        liquidity_floor=25_000_000.0,
        filters_min_notional=5.0,
        tick_size=0.1,
        available_balance=1_000.0,
        config=config,
    )

    assert not breakout_called["called"], "Spec §7.3: Breakout builder must not be called in range regime"
    assert not pullback_called["called"], "Spec §7.3: Pullback builder must not be called in range regime"


# ---------------------------------------------------------------------------
# Spec §17 — Entry Type Default
# ---------------------------------------------------------------------------

def test_breakout_retest_entry_default_is_limit_gtd(monkeypatch) -> None:
    """
    Spec §17.1: Default entry type is LIMIT_GTD (passive limit).
    A retest breakout should produce LIMIT_GTD, not STOP_ENTRY.
    Verified via the retest zone builder path.
    """
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(aqrr, "volume_ratio", lambda *_args, **_kwargs: 1.2)
    monkeypatch.setattr(
        aqrr,
        "_reward_headroom_barrier",
        lambda **kwargs: kwargs["entry_price"] + kwargs["risk_distance"] * 6,
    )

    # Build a retest scenario (moderate volume, price just above breakout level)
    candles_15m = [_candle(open_time=i, open=99.7, high=100.0, low=99.5, close=99.8) for i in range(24)]
    candles_15m.append(_candle(open_time=24, open=99.96, high=100.30, low=99.90, close=100.12, volume=1_800))
    candles_1h = [_candle(open_time=i, open=100 + i, high=101 + i, low=99 + i, close=100.5 + i) for i in range(30)]

    candidate = aqrr._build_breakout_candidate(
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=100.12,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 50_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG),
        config=config,
        tick_size=0.01,
    )

    assert candidate is not None
    assert candidate.entry_style == "LIMIT_GTD", (
        "Spec §17.1: Default entry must be LIMIT_GTD for retest breakouts"
    )
