import pytest

import app.services.strategy.indicators as indicators
import app.services.strategy.aqrr as aqrr
from app.models.enums import ScanSymbolOutcome, SignalDirection
from app.services.strategy.adx import TrendMetrics
from app.services.strategy.config import resolve_strategy_config
from app.services.strategy.types import Candle, SetupCandidate


def _candles_from_closes(closes: list[float], *, spread: float = 0.8) -> list[Candle]:
    candles: list[Candle] = []
    for index, close in enumerate(closes):
        candles.append(
            Candle(
                open_time=index,
                open=close - 0.2,
                high=close + (spread / 2),
                low=close - (spread / 2),
                close=close,
                volume=1_000 + index * 5,
            )
        )
    return candles


def _default_config():
    return resolve_strategy_config({})


def _market_state(state: str, direction: SignalDirection | None) -> aqrr.MarketStateAssessment:
    return aqrr.MarketStateAssessment(
        market_state=state,
        direction=direction,
        ema_50_1h=100.0,
        ema_200_1h=95.0,
        ema_50_4h=120.0,
        ema_200_4h=110.0,
        adx_1h=30.0,
        ema_slope_norm_1h=1.0,
        bollinger_bandwidth_1h=0.02,
        mean_cross_count_1h=1,
        volatility_shock=False,
        diagnostics={},
    )


def _candidate(
    *,
    symbol: str,
    direction: SignalDirection,
    setup_family: str,
    setup_variant: str,
    final_score: int,
    rank_value: float,
) -> SetupCandidate:
    return SetupCandidate(
        symbol=symbol,
        direction=direction,
        entry_price=100.0,
        stop_loss=96.0,
        take_profit=114.0,
        actual_rr=3.5,
        net_r_multiple=3.25,
        estimated_cost=0.15,
        confirmation_score=82,
        final_score=final_score,
        rank_value=rank_value,
        setup_family=setup_family,
        setup_variant=setup_variant,
        entry_style="LIMIT_GTD",
        market_state=aqrr.MARKET_STATE_BULL,
        execution_tier="TIER_A",
        score_breakdown={
            "structure_quality": 22,
            "regime_alignment": 18,
            "liquidity_execution_quality": 14,
            "reward_headroom_quality": 9,
        },
        reason_text="aqrr test candidate",
        current_price=101.0,
        swing_origin=95.0,
        swing_terminus=110.0,
    )


def test_volatility_shock_uses_exact_30_day_atr_percentile_window(monkeypatch) -> None:
    atr_period = 14
    older_history_count = 600
    lookback_bars = indicators.VOLATILITY_SHOCK_15M_LOOKBACK_BARS
    recent_le_count = 2777
    recent_gt_count = (lookback_bars - 1) - recent_le_count
    atr_history = ([0.03] * older_history_count) + ([0.05] * recent_le_count) + ([0.08] * recent_gt_count) + [0.06]
    candles = _candles_from_closes([100.0] * (len(atr_history) + atr_period), spread=1.0)
    closes = [1.0] * len(candles)

    def fake_calculate_atr(candle_slice, period: int):
        index = len(candle_slice) - (period + 1)
        return atr_history[index]

    monkeypatch.setattr(indicators, "calculate_atr", fake_calculate_atr)
    monkeypatch.setattr(indicators, "closes", lambda _candles: closes)

    shock, diagnostics = indicators.volatility_shock_flag(candles, atr_period=atr_period, range_multiple=2.5)

    expected_window_percentile = (recent_le_count + 1) / lookback_bars
    full_history_percentile = (recent_le_count + 1 + older_history_count) / len(atr_history)
    assert full_history_percentile > 0.97
    assert expected_window_percentile < 0.97
    assert shock is False
    assert diagnostics["atr_percentile"] == pytest.approx(expected_window_percentile, rel=1e-9, abs=1e-9)
    assert diagnostics["atr_percentile_window_bars"] == float(lookback_bars)


def test_classify_market_state_detects_bull_trend(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(
        aqrr,
        "calculate_trend_metrics",
        lambda *_args, **_kwargs: TrendMetrics(adx=32.0, plus_di=28.0, minus_di=10.0, di_spread=18.0),
    )
    monkeypatch.setattr(aqrr, "volatility_shock_flag", lambda *_args, **_kwargs: (False, {"volatility_shock": False}))

    candles_15m = _candles_from_closes([100 + index * 0.25 for index in range(260)])
    candles_1h = _candles_from_closes([100 + index * 0.60 for index in range(260)])
    candles_4h = _candles_from_closes([100 + index * 1.20 for index in range(260)])

    assessment = aqrr.classify_market_state(
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        config=config,
    )

    assert assessment.market_state == aqrr.MARKET_STATE_BULL
    assert assessment.direction == SignalDirection.LONG


def test_classify_market_state_detects_balanced_range(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(
        aqrr,
        "calculate_trend_metrics",
        lambda *_args, **_kwargs: TrendMetrics(adx=14.0, plus_di=16.0, minus_di=15.0, di_spread=1.0),
    )
    monkeypatch.setattr(aqrr, "volatility_shock_flag", lambda *_args, **_kwargs: (False, {"volatility_shock": False}))
    monkeypatch.setattr(aqrr, "bollinger_bandwidth", lambda *_args, **_kwargs: 0.02)
    monkeypatch.setattr(aqrr, "historical_bollinger_bandwidths", lambda *_args, **_kwargs: [0.03] * 60)
    monkeypatch.setattr(aqrr, "mean_cross_count", lambda *_args, **_kwargs: 5)

    base = [100.0, 100.6, 99.4, 100.5, 99.5, 100.4]
    closes = [base[index % len(base)] for index in range(260)]
    candles = _candles_from_closes(closes, spread=1.0)

    assessment = aqrr.classify_market_state(
        candles_15m=candles,
        candles_1h=candles,
        candles_4h=candles,
        config=config,
    )

    assert assessment.market_state == aqrr.MARKET_STATE_RANGE
    assert assessment.direction is None


def test_classify_market_state_requires_recent_range_containment(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(
        aqrr,
        "calculate_trend_metrics",
        lambda *_args, **_kwargs: TrendMetrics(adx=14.0, plus_di=16.0, minus_di=15.0, di_spread=1.0),
    )
    monkeypatch.setattr(aqrr, "volatility_shock_flag", lambda *_args, **_kwargs: (False, {"volatility_shock": False}))
    monkeypatch.setattr(aqrr, "bollinger_bandwidth", lambda *_args, **_kwargs: 0.02)
    monkeypatch.setattr(aqrr, "historical_bollinger_bandwidths", lambda *_args, **_kwargs: [0.03] * 60)
    monkeypatch.setattr(aqrr, "mean_cross_count", lambda *_args, **_kwargs: 5)
    monkeypatch.setattr(
        aqrr,
        "_range_containment_1h",
        lambda *_args, **_kwargs: (False, {"range_contained_1h": False}),
    )

    base = [100.0, 100.6, 99.4, 100.5, 99.5, 100.4]
    closes = [base[index % len(base)] for index in range(260)]
    candles = _candles_from_closes(closes, spread=1.0)

    assessment = aqrr.classify_market_state(
        candles_15m=candles,
        candles_1h=candles,
        candles_4h=candles,
        config=config,
    )

    assert assessment.market_state == aqrr.MARKET_STATE_UNSTABLE
    assert assessment.diagnostics["range_contained_1h"] is False


def test_evaluate_symbol_routes_bull_candidates_and_sorts_by_rank(monkeypatch) -> None:
    config = _default_config()
    breakout = _candidate(
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        setup_family=aqrr.SETUP_BREAKOUT,
        setup_variant="trend_breakout_retest",
        final_score=82,
        rank_value=82.0,
    )
    pullback = _candidate(
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        setup_family=aqrr.SETUP_PULLBACK,
        setup_variant="trend_pullback_continuation",
        final_score=91,
        rank_value=91.0,
    )

    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG))
    monkeypatch.setattr(aqrr, "_build_breakout_candidate", lambda **_kwargs: breakout)
    monkeypatch.setattr(aqrr, "_build_pullback_candidate", lambda **_kwargs: pullback)

    evaluation = aqrr.evaluate_symbol(
        symbol="BTCUSDT",
        candles_15m=_candles_from_closes([100.0] * 80),
        candles_1h=_candles_from_closes([100.0 + index * 0.1 for index in range(80)]),
        candles_4h=_candles_from_closes([100.0 + index * 0.2 for index in range(80)]),
        current_price=101.0,
        funding_rate=0.0,
        quote_volume=50_000_000.0,
        spread_bps=5.0,
        liquidity_floor=25_000_000.0,
        filters_min_notional=5.0,
        tick_size=0.1,
        available_balance=1_000.0,
        config=config,
        btc_returns_1h=[0.01, 0.02, 0.03],
    )

    assert evaluation.outcome == ScanSymbolOutcome.CANDIDATE
    assert evaluation.direction == SignalDirection.LONG
    assert [candidate.setup_family for candidate in evaluation.candidates] == [aqrr.SETUP_PULLBACK, aqrr.SETUP_BREAKOUT]
    assert evaluation.candidates[0].extra_context["market_state"] == aqrr.MARKET_STATE_BULL
    assert evaluation.candidates[0].extra_context["execution_tier"] == "TIER_A"


def test_evaluate_symbol_returns_unstable_no_trade(monkeypatch) -> None:
    config = _default_config()
    unstable_state = aqrr.MarketStateAssessment(
        market_state=aqrr.MARKET_STATE_UNSTABLE,
        direction=None,
        ema_50_1h=100.0,
        ema_200_1h=100.0,
        ema_50_4h=100.0,
        ema_200_4h=100.0,
        adx_1h=18.0,
        ema_slope_norm_1h=0.0,
        bollinger_bandwidth_1h=0.02,
        mean_cross_count_1h=4,
        volatility_shock=True,
        diagnostics={"volatility_shock": True},
    )
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: unstable_state)

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

    assert evaluation.outcome == ScanSymbolOutcome.NO_SETUP
    assert evaluation.reason_text == "unstable_no_trade"
    assert evaluation.filter_reasons == ["unstable_no_trade"]


def test_evaluate_symbol_returns_raw_builder_rejection_reasons(monkeypatch) -> None:
    config = _default_config()
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG))
    monkeypatch.setattr(
        aqrr,
        "_build_breakout_candidate",
        lambda **_kwargs: aqrr.CandidateBuildResult(
            candidate=None,
            raw_rejection_reasons=("breakout_too_extended",),
            rejection_stage="candidate_build",
            setup_diagnostic={"setup_family": aqrr.SETUP_BREAKOUT, "entry_types_considered": ["LIMIT_GTD", "STOP_ENTRY"]},
        ),
    )
    monkeypatch.setattr(
        aqrr,
        "_build_pullback_candidate",
        lambda **_kwargs: aqrr.CandidateBuildResult(
            candidate=None,
            raw_rejection_reasons=("pullback_no_rejection_evidence",),
            rejection_stage="candidate_build",
            setup_diagnostic={"setup_family": aqrr.SETUP_PULLBACK, "entry_types_considered": ["LIMIT_GTD", "STOP_ENTRY"]},
        ),
    )

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

    assert evaluation.outcome == ScanSymbolOutcome.NO_SETUP
    assert evaluation.filter_reasons == ["breakout_too_extended", "pullback_no_rejection_evidence"]
    assert evaluation.diagnostic["aqrr_raw_rejection_reason"] == "breakout_too_extended"
    assert evaluation.diagnostic["aqrr_rejection_stage"] == "candidate_build"


def test_evaluate_symbol_returns_required_leverage_rejection_reason(monkeypatch) -> None:
    config = _default_config()
    breakout = _candidate(
        symbol="BTCUSDT",
        direction=SignalDirection.LONG,
        setup_family=aqrr.SETUP_BREAKOUT,
        setup_variant="trend_breakout_retest",
        final_score=88,
        rank_value=88.0,
    )
    monkeypatch.setattr(aqrr, "classify_market_state", lambda **_kwargs: _market_state(aqrr.MARKET_STATE_BULL, SignalDirection.LONG))
    monkeypatch.setattr(aqrr, "_build_breakout_candidate", lambda **_kwargs: breakout)
    monkeypatch.setattr(aqrr, "_build_pullback_candidate", lambda **_kwargs: None)
    monkeypatch.setattr(aqrr, "_required_leverage", lambda **_kwargs: (500.0, config.max_leverage + 1))

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

    assert evaluation.outcome == ScanSymbolOutcome.FILTERED_OUT
    assert evaluation.filter_reasons == ["required_leverage_above_max"]
    assert evaluation.diagnostic["aqrr_raw_rejection_reason"] == "required_leverage_above_max"
