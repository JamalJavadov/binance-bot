from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import app.services.strategy.aqrr as aqrr
from app.models.enums import ScanSymbolOutcome, SignalDirection
from app.services.strategy.config import resolve_strategy_config
from app.services.strategy.types import Candle, SetupCandidate


def _default_config():
    return resolve_strategy_config({})


def _candle(*, open_time: int, open: float, high: float, low: float, close: float, volume: float = 1_000) -> Candle:
    return Candle(open_time=open_time, open=open, high=high, low=low, close=close, volume=volume)


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
    cluster: str | None,
    btc_beta: float,
    returns_1h: list[float],
    direction: SignalDirection = SignalDirection.LONG,
    net_r_multiple: float = 3.2,
    estimated_cost: float = 0.10,
    execution_tier: str = "TIER_A",
    regime_alignment: int = 18,
    higher_timeframe_structure_quality: float | None = None,
) -> SetupCandidate:
    selection_context = {
        "cluster": cluster,
        "btc_beta_correlation": btc_beta,
        "returns_1h": returns_1h,
    }
    if higher_timeframe_structure_quality is not None:
        selection_context["higher_timeframe_structure_quality"] = higher_timeframe_structure_quality
    return SetupCandidate(
        symbol=symbol,
        direction=direction,
        entry_price=100.0 + rank_value,
        stop_loss=95.0 + rank_value,
        take_profit=118.0 + rank_value,
        actual_rr=3.6,
        net_r_multiple=net_r_multiple,
        estimated_cost=estimated_cost,
        confirmation_score=80,
        final_score=int(rank_value),
        rank_value=rank_value,
        setup_family=aqrr.SETUP_BREAKOUT,
        setup_variant="ranked_candidate",
        entry_style="LIMIT_GTD",
        market_state=aqrr.MARKET_STATE_BULL,
        execution_tier=execution_tier,
        score_breakdown={
            "structure_quality": 22,
            "regime_alignment": regime_alignment,
            "liquidity_execution_quality": 14,
            "reward_headroom_quality": 9,
        },
        selection_context=selection_context,
    )


def test_build_breakout_candidate_constructs_stop_entry_setup(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(aqrr, "volume_ratio", lambda *_args, **_kwargs: 1.6)
    monkeypatch.setattr(
        aqrr,
        "_reward_headroom_barrier",
        lambda **kwargs: kwargs["entry_price"] + kwargs["risk_distance"] * 6,
    )
    candles_15m = [
        _candle(open_time=index, open=99.7, high=100.0, low=99.5, close=99.8)
        for index in range(24)
    ]
    candles_15m.append(_candle(open_time=24, open=99.95, high=100.55, low=99.90, close=100.45, volume=3_000))
    candles_1h = [_candle(open_time=index, open=100 + index, high=101 + index, low=99 + index, close=100.5 + index) for index in range(30)]

    candidate = aqrr._build_breakout_candidate(
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=100.45,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 50_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG),
        config=config,
        tick_size=0.01,
    )

    assert candidate is not None
    assert candidate.setup_family == aqrr.SETUP_BREAKOUT
    assert candidate.entry_style == "STOP_ENTRY"
    assert candidate.net_r_multiple >= 3.0
    assert candidate.expiry_minutes == config.breakout_retest_expiry_bars * 15


def test_classify_market_state_populates_one_hour_atr_percentile() -> None:
    config = _default_config()
    candles_15m = [
        _candle(open_time=index, open=100 + index * 0.05, high=101 + index * 0.05, low=99 + index * 0.05, close=100.5 + index * 0.05)
        for index in range(120)
    ]
    candles_1h = [
        _candle(open_time=index, open=100 + index * 0.2, high=101.5 + index * 0.2, low=99.2 + index * 0.2, close=100.9 + index * 0.2)
        for index in range(260)
    ]
    candles_4h = [
        _candle(open_time=index, open=100 + index * 0.5, high=102 + index * 0.5, low=99 + index * 0.5, close=101 + index * 0.5)
        for index in range(260)
    ]

    assessment = aqrr.classify_market_state(
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        config=config,
    )

    assert assessment.diagnostics["atr_percentile"] is not None
    assert 0.0 <= float(assessment.diagnostics["atr_percentile"]) <= 1.0


def test_classify_market_state_flags_pump_dump_profile_as_unstable(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "volatility_shock_flag", lambda *_args, **_kwargs: (False, {"volatility_shock": False}))
    candles_15m = [
        _candle(
            open_time=index,
            open=100 + index * 0.02,
            high=100.5 + index * 0.02,
            low=99.5 + index * 0.02,
            close=100.2 + index * 0.02,
        )
        for index in range(252)
    ]
    base = candles_15m[-1].close
    for index in range(252, 260):
        step = index - 251
        candles_15m.append(
            _candle(
                open_time=index,
                open=base + (step * 1.2),
                high=base + (step * 1.9),
                low=base + (step * 0.6),
                close=base + (step * 1.5),
            )
        )
    candles_1h = [_candle(open_time=index, open=100 + index * 0.2, high=101.5 + index * 0.2, low=99.2 + index * 0.2, close=100.9 + index * 0.2) for index in range(260)]
    candles_4h = [_candle(open_time=index, open=100 + index * 0.5, high=102 + index * 0.5, low=99 + index * 0.5, close=101 + index * 0.5) for index in range(260)]

    assessment = aqrr.classify_market_state(
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        config=config,
    )

    assert assessment.market_state == aqrr.MARKET_STATE_UNSTABLE
    assert assessment.diagnostics["pump_dump_profile"] is True


def test_classify_market_state_flags_severe_spread_and_liquidity_degradation_as_unstable() -> None:
    config = _default_config()
    candles_15m = [
        _candle(open_time=index, open=100 + index * 0.03, high=100.4 + index * 0.03, low=99.6 + index * 0.03, close=100.2 + index * 0.03)
        for index in range(260)
    ]
    candles_1h = [_candle(open_time=index, open=100 + index * 0.2, high=101.2 + index * 0.2, low=99.2 + index * 0.2, close=100.8 + index * 0.2) for index in range(260)]
    candles_4h = [_candle(open_time=index, open=100 + index * 0.4, high=101.8 + index * 0.4, low=99.4 + index * 0.4, close=100.9 + index * 0.4) for index in range(260)]

    assessment = aqrr.classify_market_state(
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        config=config,
        spread_bps=30.0,
        quote_volume=9_000_000.0,
        liquidity_floor=25_000_000.0,
        spread_relative_ratio=3.4,
        relative_spread_ready=True,
    )

    assert assessment.market_state == aqrr.MARKET_STATE_UNSTABLE
    assert assessment.diagnostics["spread_liquidity_unstable"] is True
    assert "severe_spread_degradation" in assessment.diagnostics["spread_liquidity_unstable_reasons"]
    assert "liquidity_degradation" in assessment.diagnostics["spread_liquidity_unstable_reasons"]


def test_build_breakout_candidate_uses_retest_zone_for_limit_entry(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(aqrr, "volume_ratio", lambda *_args, **_kwargs: 1.2)
    monkeypatch.setattr(
        aqrr,
        "_reward_headroom_barrier",
        lambda **kwargs: kwargs["entry_price"] + kwargs["risk_distance"] * 6,
    )
    candles_15m = [
        _candle(open_time=index, open=99.7, high=100.0, low=99.5, close=99.8)
        for index in range(24)
    ]
    candles_15m.append(_candle(open_time=24, open=99.96, high=100.30, low=99.90, close=100.12, volume=1_800))
    candles_1h = [_candle(open_time=index, open=100 + index, high=101 + index, low=99 + index, close=100.5 + index) for index in range(30)]

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
    assert candidate.entry_style == "LIMIT_GTD"
    strategy_context = candidate.extra_context["strategy_context"]
    assert candidate.entry_price == pytest.approx(100.05)
    assert strategy_context["retest_entry_zone_low"] == pytest.approx(100.0)
    assert strategy_context["retest_entry_zone_high"] == pytest.approx(100.1)
    assert strategy_context["retest_entry_zone_low"] <= candidate.entry_price <= strategy_context["retest_entry_zone_high"]


def test_build_pullback_candidate_constructs_trend_continuation_setup(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(
        aqrr,
        "ema_series",
        lambda values, period: ([100.0] * len(values)) if period == config.ema_fast_period else ([99.5] * len(values)),
    )
    monkeypatch.setattr(
        aqrr,
        "_reward_headroom_barrier",
        lambda **kwargs: kwargs["entry_price"] + kwargs["risk_distance"] * 6,
    )
    candles_15m = [_candle(open_time=index, open=100.6, high=101.0, low=100.1, close=100.7) for index in range(58)]
    candles_15m.append(_candle(open_time=58, open=99.4, high=99.7, low=99.3, close=99.6))
    candles_15m.append(_candle(open_time=59, open=99.6, high=100.4, low=99.4, close=100.2, volume=1_500))
    candles_1h = [_candle(open_time=index, open=100.0, high=101.2, low=99.8, close=101.0) for index in range(30)]

    candidate = aqrr._build_pullback_candidate(
        symbol="ETHUSDT",
        direction=SignalDirection.LONG,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=100.2,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 50_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG),
        config=config,
        tick_size=0.01,
    )

    assert candidate is not None
    assert candidate.setup_family == aqrr.SETUP_PULLBACK
    assert candidate.direction == SignalDirection.LONG
    assert candidate.entry_style == "STOP_ENTRY"
    assert round(candidate.net_r_multiple, 4) >= 3.0


def test_build_range_candidate_constructs_reversion_setup(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(aqrr, "rsi", lambda *_args, **_kwargs: 40.0)
    candles_15m = [_candle(open_time=index, open=100.0, high=101.0, low=99.0, close=100.0) for index in range(23)]
    candles_15m.append(_candle(open_time=23, open=99.1, high=99.5, low=99.0, close=99.4, volume=1_200))

    candidate = aqrr._build_range_candidate(
        symbol="SOLUSDT",
        candles_15m=candles_15m,
        current_price=99.4,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 50_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_RANGE, None),
        config=config,
        tick_size=0.01,
    )

    assert candidate is not None
    assert candidate.setup_family == aqrr.SETUP_RANGE
    assert candidate.direction == SignalDirection.LONG
    assert candidate.entry_style == "LIMIT_GTD"
    assert candidate.net_r_multiple >= 3.0


def test_evaluate_symbol_rejects_tier_c_execution(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG))

    evaluation = aqrr.evaluate_symbol(
        symbol="BTCUSDT",
        candles_15m=[],
        candles_1h=[],
        candles_4h=[],
        current_price=100.0,
        funding_rate=0.0,
        quote_volume=10_000_000.0,
        spread_bps=13.0,
        liquidity_floor=25_000_000.0,
        filters_min_notional=5.0,
        tick_size=0.1,
        available_balance=1_000.0,
        config=config,
    )

    assert evaluation.outcome == ScanSymbolOutcome.FILTERED_OUT
    assert evaluation.filter_reasons == ["execution_tier_c_rejected"]


def test_required_leverage_uses_remaining_slot_and_risk_budget() -> None:
    config = _default_config()

    _planned_notional_all_slots, leverage_all_slots = aqrr._required_leverage(
        entry_price=100.0,
        stop_loss=99.0,
        available_balance=90.0,
        filters_min_notional=5.0,
        config=config,
        remaining_entry_slots=3,
        remaining_portfolio_risk_usdt=5.4,
    )
    _planned_notional_last_slot, leverage_last_slot = aqrr._required_leverage(
        entry_price=100.0,
        stop_loss=99.0,
        available_balance=90.0,
        filters_min_notional=5.0,
        config=config,
        remaining_entry_slots=1,
        remaining_portfolio_risk_usdt=5.4,
    )

    assert leverage_last_slot < leverage_all_slots


def test_estimated_cost_prefers_limit_entries_and_only_applies_relevant_funding() -> None:
    config = _default_config()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    limit_cost = aqrr._estimated_cost_distance(
        entry_price=100.0,
        spread_bps=8.0,
        funding_rate=0.0005,
        next_funding_time_ms=int(datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc).timestamp() * 1000),
        direction=SignalDirection.LONG,
        entry_style="LIMIT_GTD",
        setup_family=aqrr.SETUP_BREAKOUT,
        config=config,
        now=now,
    )
    stop_cost = aqrr._estimated_cost_distance(
        entry_price=100.0,
        spread_bps=8.0,
        funding_rate=0.0005,
        next_funding_time_ms=int(datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc).timestamp() * 1000),
        direction=SignalDirection.LONG,
        entry_style="STOP_ENTRY",
        setup_family=aqrr.SETUP_BREAKOUT,
        config=config,
        now=now,
    )
    no_funding_cost = aqrr._estimated_cost_distance(
        entry_price=100.0,
        spread_bps=8.0,
        funding_rate=0.0005,
        next_funding_time_ms=int(datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),
        direction=SignalDirection.LONG,
        entry_style="LIMIT_GTD",
        setup_family=aqrr.SETUP_RANGE,
        config=config,
        now=now,
    )

    assert limit_cost < stop_cost
    assert no_funding_cost < limit_cost


def test_account_specific_commission_can_flip_net_three_r_acceptance() -> None:
    config = _default_config()
    risk_distance = 1.0
    available_reward = 3.8

    low_cost = aqrr._estimated_cost_distance(
        entry_price=100.0,
        spread_bps=8.0,
        funding_rate=0.0,
        next_funding_time_ms=None,
        direction=SignalDirection.LONG,
        entry_style="LIMIT_GTD",
        setup_family=aqrr.SETUP_BREAKOUT,
        config=config,
        account_maker_fee_rate=0.0002,
        account_taker_fee_rate=0.0004,
    )
    high_cost = aqrr._estimated_cost_distance(
        entry_price=100.0,
        spread_bps=8.0,
        funding_rate=0.0,
        next_funding_time_ms=None,
        direction=SignalDirection.LONG,
        entry_style="LIMIT_GTD",
        setup_family=aqrr.SETUP_BREAKOUT,
        config=config,
        account_maker_fee_rate=0.0020,
        account_taker_fee_rate=0.0025,
    )

    required_low = aqrr._required_reward_distance(
        risk_distance=risk_distance,
        estimated_cost=low_cost,
        min_net_r_multiple=float(config.min_net_r_multiple),
    )
    required_high = aqrr._required_reward_distance(
        risk_distance=risk_distance,
        estimated_cost=high_cost,
        min_net_r_multiple=float(config.min_net_r_multiple),
    )

    assert required_low < available_reward
    assert required_high > available_reward


def test_estimated_cost_uses_adverse_funding_history_when_next_funding_is_unknown() -> None:
    config = _default_config()

    base_cost = aqrr._estimated_cost_distance(
        entry_price=100.0,
        spread_bps=8.0,
        funding_rate=0.0,
        next_funding_time_ms=None,
        direction=SignalDirection.LONG,
        entry_style="LIMIT_GTD",
        setup_family=aqrr.SETUP_PULLBACK,
        config=config,
        funding_rate_history=[],
    )
    history_cost = aqrr._estimated_cost_distance(
        entry_price=100.0,
        spread_bps=8.0,
        funding_rate=0.0,
        next_funding_time_ms=None,
        direction=SignalDirection.LONG,
        entry_style="LIMIT_GTD",
        setup_family=aqrr.SETUP_PULLBACK,
        config=config,
        funding_rate_history=[0.0009, 0.0008, 0.0007, -0.0002],
    )

    assert history_cost > base_cost
    assert aqrr._required_reward_distance(
        risk_distance=1.0,
        estimated_cost=history_cost,
        min_net_r_multiple=float(config.min_net_r_multiple),
    ) > aqrr._required_reward_distance(
        risk_distance=1.0,
        estimated_cost=base_cost,
        min_net_r_multiple=float(config.min_net_r_multiple),
    )


def test_evaluate_symbol_applies_net_three_r_hard_gate(monkeypatch) -> None:
    config = _default_config()
    failing_candidate = SetupCandidate(
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=110.0,
        actual_rr=2.5,
        net_r_multiple=2.5,
        estimated_cost=0.25,
        confirmation_score=82,
        final_score=85,
        rank_value=85.0,
        setup_family=aqrr.SETUP_BREAKOUT,
        setup_variant="too_small_after_costs",
        entry_style="LIMIT_GTD",
        market_state=aqrr.MARKET_STATE_BULL,
        execution_tier="TIER_A",
        score_breakdown={
            "structure_quality": 22,
            "regime_alignment": 18,
            "liquidity_execution_quality": 14,
            "reward_headroom_quality": 9,
        },
    )
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG))
    monkeypatch.setattr(aqrr, "_build_breakout_candidate", lambda **_kwargs: failing_candidate)
    monkeypatch.setattr(aqrr, "_build_pullback_candidate", lambda **_kwargs: None)

    evaluation = aqrr.evaluate_symbol(
        symbol="BTCUSDT",
        candles_15m=[],
        candles_1h=[_candle(open_time=index, open=100.0, high=101.0, low=99.0, close=100.5) for index in range(80)],
        candles_4h=[],
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

    assert evaluation.outcome == ScanSymbolOutcome.FILTERED_OUT
    assert evaluation.filter_reasons == ["net_r_multiple_below_min"]
    assert evaluation.diagnostic["aqrr_raw_rejection_reason"] == "net_r_multiple_below_min"


def test_build_pullback_candidate_accepts_bullish_engulf_reclaim_evidence(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(
        aqrr,
        "ema_series",
        lambda values, period: ([100.0] * len(values)) if period == config.ema_fast_period else ([99.5] * len(values)),
    )
    monkeypatch.setattr(
        aqrr,
        "_reward_headroom_barrier",
        lambda **kwargs: kwargs["entry_price"] + kwargs["risk_distance"] * 5,
    )
    candles_15m = [_candle(open_time=index, open=100.7, high=101.0, low=100.3, close=100.8) for index in range(57)]
    candles_15m.append(_candle(open_time=57, open=100.15, high=100.20, low=99.45, close=99.60))
    candles_15m.append(_candle(open_time=58, open=99.50, high=100.60, low=99.45, close=100.40))
    candles_1h = [_candle(open_time=index, open=100.0, high=101.2, low=99.8, close=101.0) for index in range(30)]

    candidate = aqrr._build_pullback_candidate(
        symbol="ETHUSDT",
        direction=SignalDirection.LONG,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=100.4,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 60_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG),
        config=config,
        tick_size=0.01,
    )

    assert candidate is not None
    assert candidate.setup_family == aqrr.SETUP_PULLBACK
    assert candidate.extra_context["strategy_context"]["rejection_evidence"]["engulf_or_reclaim"] is True


def test_build_pullback_candidate_accepts_short_local_lower_high_evidence(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(
        aqrr,
        "ema_series",
        lambda values, period: ([100.0] * len(values)) if period == config.ema_fast_period else ([100.5] * len(values)),
    )
    monkeypatch.setattr(
        aqrr,
        "_reward_headroom_barrier",
        lambda **kwargs: kwargs["entry_price"] - kwargs["risk_distance"] * 5,
    )
    candles_15m = [_candle(open_time=index, open=99.6, high=99.9, low=99.2, close=99.5) for index in range(56)]
    candles_15m.extend(
        [
            _candle(open_time=56, open=99.9, high=100.6, low=99.8, close=100.2),
            _candle(open_time=57, open=100.2, high=100.8, low=100.0, close=100.6),
            _candle(open_time=58, open=100.5, high=100.7, low=99.8, close=100.0),
        ]
    )
    market_state = aqrr.MarketStateAssessment(
        market_state=aqrr.MARKET_STATE_BEAR,
        direction=SignalDirection.SHORT,
        ema_50_1h=100.5,
        ema_200_1h=101.0,
        ema_50_4h=99.0,
        ema_200_4h=100.0,
        adx_1h=30.0,
        ema_slope_norm_1h=-1.0,
        bollinger_bandwidth_1h=0.02,
        mean_cross_count_1h=1,
        volatility_shock=False,
        diagnostics={},
    )
    candles_1h = [_candle(open_time=index, open=100.5, high=100.7, low=99.6, close=100.0) for index in range(30)]

    candidate = aqrr._build_pullback_candidate(
        symbol="ETHUSDT",
        direction=SignalDirection.SHORT,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=100.0,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 60_000_000.0, 5.0, 25_000_000.0),
        market_state=market_state,
        config=config,
        tick_size=0.01,
    )

    assert candidate is not None
    assert candidate.direction == SignalDirection.SHORT
    assert candidate.extra_context["strategy_context"]["rejection_evidence"]["local_structure_recovery"] is True


def test_build_pullback_candidate_accepts_countertrend_momentum_loss_evidence(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(
        aqrr,
        "ema_series",
        lambda values, period: ([100.0] * len(values)) if period == config.ema_fast_period else ([99.5] * len(values)),
    )
    monkeypatch.setattr(
        aqrr,
        "_reward_headroom_barrier",
        lambda **kwargs: kwargs["entry_price"] + kwargs["risk_distance"] * 6,
    )
    candles_15m = [_candle(open_time=index, open=100.8, high=101.0, low=100.2, close=100.7) for index in range(56)]
    candles_15m.extend(
        [
            _candle(open_time=56, open=100.4, high=100.5, low=99.6, close=99.8),
            _candle(open_time=57, open=99.9, high=100.0, low=99.5, close=99.6),
            _candle(open_time=58, open=99.7, high=100.1, low=99.5, close=99.8),
        ]
    )
    candles_1h = [_candle(open_time=index, open=100.0, high=101.0, low=99.7, close=100.8) for index in range(30)]

    candidate = aqrr._build_pullback_candidate(
        symbol="ETHUSDT",
        direction=SignalDirection.LONG,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=99.8,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 60_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG),
        config=config,
        tick_size=0.01,
    )

    assert candidate is not None
    assert candidate.extra_context["strategy_context"]["rejection_evidence"]["countertrend_momentum_loss"] is True


def test_build_pullback_candidate_with_diagnostics_reports_zone_subreason(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(
        aqrr,
        "ema_series",
        lambda values, period: ([100.0] * len(values)) if period == config.ema_fast_period else ([99.5] * len(values)),
    )
    candles_15m = [_candle(open_time=index, open=101.0, high=101.4, low=100.7, close=101.1) for index in range(59)]
    candles_1h = [_candle(open_time=index, open=100.0, high=101.0, low=99.8, close=100.8) for index in range(30)]

    result = aqrr._build_pullback_candidate(
        symbol="ETHUSDT",
        direction=SignalDirection.LONG,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=101.1,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 60_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG),
        config=config,
        tick_size=0.01,
        with_diagnostics=True,
    )

    assert isinstance(result, aqrr.CandidateBuildResult)
    assert result.candidate is None
    assert result.raw_rejection_reasons == ("pullback_zone_not_touched",)


def test_build_breakout_candidate_with_diagnostics_reports_participation_subreason(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "calculate_atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(aqrr, "volume_ratio", lambda *_args, **_kwargs: 1.0)
    candles_15m = [
        _candle(open_time=index, open=99.7, high=100.0, low=99.5, close=99.8)
        for index in range(24)
    ]
    candles_15m.append(_candle(open_time=24, open=99.98, high=100.20, low=99.95, close=100.05, volume=1_000))
    candles_1h = [_candle(open_time=index, open=100 + index, high=101 + index, low=99 + index, close=100.5 + index) for index in range(30)]

    result = aqrr._build_breakout_candidate(
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        current_price=100.05,
        funding_rate=0.0,
        execution_tier=aqrr.ExecutionTierAssessment("TIER_A", 50_000_000.0, 5.0, 25_000_000.0),
        market_state=_market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG),
        config=config,
        tick_size=0.01,
        with_diagnostics=True,
    )

    assert isinstance(result, aqrr.CandidateBuildResult)
    assert result.candidate is None
    assert result.raw_rejection_reasons == ("breakout_participation_filter_failed",)


def test_select_candidates_rejects_correlation_conflict() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="BTCUSDT", rank_value=95.0, cluster="majors", btc_beta=0.25, returns_1h=[0.01, 0.02, 0.03]),
            _candidate(symbol="ETHUSDT", rank_value=90.0, cluster="alts", btc_beta=0.20, returns_1h=[0.02, 0.04, 0.06]),
        ],
        config=config,
    )

    rejected_key = ("ETHUSDT", "LONG", round(190.0, 8))
    assert [candidate.symbol for candidate in selected.selected] == ["BTCUSDT"]
    assert selected.rejected[rejected_key] == "correlation_conflict"


def test_select_candidates_tie_break_prefers_higher_net_r_for_near_equal_rank() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="LOWRUSDT", rank_value=90.10, cluster=None, btc_beta=0.20, returns_1h=[0.01, 0.02, 0.01], net_r_multiple=3.2, estimated_cost=0.05),
            _candidate(symbol="HIGHRUSDT", rank_value=90.00, cluster=None, btc_beta=0.20, returns_1h=[0.02, -0.01, 0.03], net_r_multiple=3.8, estimated_cost=0.20),
        ],
        config=config,
    )

    assert [candidate.symbol for candidate in selected.selected] == ["HIGHRUSDT", "LOWRUSDT"]


def test_select_candidates_tie_break_prefers_lower_cost_after_net_r() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="HIGHCOSTUSDT", rank_value=90.10, cluster=None, btc_beta=0.20, returns_1h=[0.01, 0.02, -0.01], net_r_multiple=3.5, estimated_cost=0.20),
            _candidate(symbol="LOWCOSTUSDT", rank_value=90.00, cluster=None, btc_beta=0.20, returns_1h=[0.02, -0.02, 0.01], net_r_multiple=3.5, estimated_cost=0.05),
        ],
        config=config,
    )

    assert [candidate.symbol for candidate in selected.selected] == ["LOWCOSTUSDT", "HIGHCOSTUSDT"]


def test_select_candidates_tie_break_prefers_lower_correlation_to_selected_positions() -> None:
    config = replace(_default_config(), correlation_reject_threshold=Decimal("1.0"))
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="ANCHORUSDT", rank_value=96.0, cluster=None, btc_beta=0.20, returns_1h=[0.03, 0.01, 0.02, 0.04, 0.05]),
            _candidate(
                symbol="HIGHCORRUSDT",
                rank_value=90.10,
                cluster=None,
                btc_beta=0.20,
                returns_1h=[0.031, 0.011, 0.021, 0.039, 0.051],
                net_r_multiple=3.5,
                estimated_cost=0.10,
            ),
            _candidate(
                symbol="LOWCORRUSDT",
                rank_value=90.00,
                cluster=None,
                btc_beta=0.20,
                returns_1h=[0.04, -0.03, 0.01, -0.02, 0.03],
                net_r_multiple=3.5,
                estimated_cost=0.10,
            ),
        ],
        config=config,
    )

    assert [candidate.symbol for candidate in selected.selected] == ["ANCHORUSDT", "LOWCORRUSDT", "HIGHCORRUSDT"]


def test_select_candidates_tie_break_prefers_liquidity_tier_then_higher_timeframe_structure() -> None:
    config = replace(_default_config(), max_entry_ideas=4)
    selected = aqrr.select_candidates(
        [
            _candidate(
                symbol="TIERAUSDT",
                rank_value=90.10,
                cluster=None,
                btc_beta=0.20,
                returns_1h=[0.01, 0.02, 0.03],
                net_r_multiple=3.5,
                estimated_cost=0.10,
                execution_tier="TIER_A",
                regime_alignment=14,
            ),
            _candidate(
                symbol="TIERBUSDT",
                rank_value=90.00,
                cluster=None,
                btc_beta=0.20,
                returns_1h=[-0.01, 0.01, 0.02],
                net_r_multiple=3.5,
                estimated_cost=0.10,
                execution_tier="TIER_B",
                regime_alignment=20,
            ),
            _candidate(
                symbol="HTFHIGHUSDT",
                rank_value=89.90,
                cluster=None,
                btc_beta=0.20,
                returns_1h=[0.02, -0.01, 0.03],
                net_r_multiple=3.5,
                estimated_cost=0.10,
                execution_tier="TIER_B",
                regime_alignment=20,
            ),
            _candidate(
                symbol="HTFLOWUSDT",
                rank_value=89.85,
                cluster=None,
                btc_beta=0.20,
                returns_1h=[0.03, -0.02, 0.01],
                net_r_multiple=3.5,
                estimated_cost=0.10,
                execution_tier="TIER_B",
                regime_alignment=12,
            ),
        ],
        config=config,
    )

    symbols = [candidate.symbol for candidate in selected.selected]
    assert symbols[0] == "TIERAUSDT"
    assert symbols.index("HTFHIGHUSDT") < symbols.index("HTFLOWUSDT")


def test_select_candidates_allows_negative_correlation_same_direction() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="BTCUSDT", rank_value=95.0, cluster="majors", btc_beta=0.25, returns_1h=[0.01, 0.02, 0.03]),
            _candidate(symbol="ETHUSDT", rank_value=90.0, cluster="majors", btc_beta=0.20, returns_1h=[-0.01, -0.02, -0.03]),
        ],
        config=config,
    )

    assert [candidate.symbol for candidate in selected.selected] == ["BTCUSDT", "ETHUSDT"]
    assert selected.rejected == {}


def test_select_candidates_allows_high_correlation_when_direction_differs() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="BTCUSDT", rank_value=95.0, cluster="majors", btc_beta=0.25, returns_1h=[0.01, 0.02, 0.03]),
            _candidate(
                symbol="ETHUSDT",
                rank_value=90.0,
                cluster="alts",
                btc_beta=0.20,
                returns_1h=[0.02, 0.04, 0.06],
                direction=SignalDirection.SHORT,
            ),
        ],
        config=config,
    )

    assert [candidate.symbol for candidate in selected.selected] == ["BTCUSDT", "ETHUSDT"]
    assert selected.rejected == {}


def test_select_candidates_rejects_cluster_conflict() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="BTCUSDT", rank_value=95.0, cluster="majors", btc_beta=0.20, returns_1h=[0.01, 0.02]),
            _candidate(symbol="ETHUSDT", rank_value=90.0, cluster="majors", btc_beta=0.20, returns_1h=[0.03, 0.01]),
            _candidate(symbol="SOLUSDT", rank_value=88.0, cluster="majors", btc_beta=0.20, returns_1h=[0.02, -0.01]),
        ],
        config=config,
    )

    rejected_key = ("SOLUSDT", "LONG", round(188.0, 8))
    assert [candidate.symbol for candidate in selected.selected] == ["BTCUSDT", "ETHUSDT"]
    assert selected.rejected[rejected_key] == "cluster_conflict"


def test_select_candidates_rejects_weak_second_candidate_from_same_cluster() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="BTCUSDT", rank_value=95.0, cluster="majors", btc_beta=0.20, returns_1h=[0.01, 0.00, -0.01]),
            _candidate(symbol="ETHUSDT", rank_value=79.0, cluster="majors", btc_beta=0.15, returns_1h=[-0.02, 0.01, 0.03]),
        ],
        config=config,
    )

    rejected_key = ("ETHUSDT", "LONG", round(179.0, 8))
    assert [candidate.symbol for candidate in selected.selected] == ["BTCUSDT"]
    assert selected.rejected[rejected_key] == "cluster_conflict"


def test_select_candidates_rejects_btc_beta_conflict() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="BTCUSDT", rank_value=95.0, cluster="majors", btc_beta=0.80, returns_1h=[0.01, 0.02]),
            _candidate(symbol="XRPUSDT", rank_value=92.0, cluster="alts", btc_beta=0.76, returns_1h=[0.03, 0.01]),
            _candidate(symbol="LINKUSDT", rank_value=89.0, cluster="growth", btc_beta=0.74, returns_1h=[0.02, -0.01]),
        ],
        config=config,
    )

    rejected_key = ("LINKUSDT", "LONG", round(189.0, 8))
    assert [candidate.symbol for candidate in selected.selected] == ["BTCUSDT", "XRPUSDT"]
    assert selected.rejected[rejected_key] == "btc_beta_conflict"


def test_select_candidates_tracks_btc_beta_by_direction() -> None:
    config = _default_config()
    selected = aqrr.select_candidates(
        [
            _candidate(symbol="BTCUSDT", rank_value=95.0, cluster="majors", btc_beta=0.80, returns_1h=[0.01, 0.02]),
            _candidate(symbol="XRPUSDT", rank_value=92.0, cluster="alts", btc_beta=0.76, returns_1h=[0.03, 0.01]),
            _candidate(
                symbol="LINKUSDT",
                rank_value=89.0,
                cluster="growth",
                btc_beta=0.74,
                returns_1h=[0.02, -0.01],
                direction=SignalDirection.SHORT,
            ),
        ],
        config=config,
    )

    assert [candidate.symbol for candidate in selected.selected] == ["BTCUSDT", "XRPUSDT", "LINKUSDT"]
    assert selected.rejected == {}


def test_cluster_for_symbol_keeps_unknown_symbols_out_of_blanket_alt_bucket() -> None:
    assert aqrr._cluster_for_symbol("BTCUSDT") == "majors"
    assert aqrr._cluster_for_symbol("ETHUSDT") == "majors"
    assert aqrr._cluster_for_symbol("ARBUSDT") == "layer2"
    assert aqrr._cluster_for_symbol("TONUSDT") is None
