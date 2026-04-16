from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cmp_to_key
from math import ceil
from statistics import median

from app.models.enums import ScanSymbolOutcome, SignalDirection
from app.services.strategy.adx import calculate_atr, calculate_trend_metrics
from app.services.strategy.config import StrategyConfig
from app.services.strategy.indicators import (
    body_fraction,
    bollinger_bandwidth,
    closes,
    ema_series,
    historical_bollinger_bandwidths,
    lower_shadow_fraction,
    mean_cross_count,
    percentile_rank,
    normalized_ema_slope,
    percentage_returns,
    recent_median_range,
    rsi,
    upper_shadow_fraction,
    volume_ratio,
    volatility_shock_flag,
)
from app.services.strategy.types import Candle, SetupCandidate


MARKET_STATE_BULL = "BULL_TREND"
MARKET_STATE_BEAR = "BEAR_TREND"
MARKET_STATE_RANGE = "BALANCED_RANGE"
MARKET_STATE_UNSTABLE = "UNSTABLE"

SETUP_BREAKOUT = "breakout_retest"
SETUP_PULLBACK = "pullback_continuation"
SETUP_RANGE = "range_reversion"

THEME_CLUSTERS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "majors",
        frozenset(
            {
                "BTCUSDT",
                "ETHUSDT",
                "BNBUSDT",
                "SOLUSDT",
                "XRPUSDT",
                "ADAUSDT",
                "DOGEUSDT",
                "TRXUSDT",
                "LINKUSDT",
                "LTCUSDT",
            }
        ),
    ),
    (
        "meme",
        frozenset({"SHIBUSDT", "PEPEUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT"}),
    ),
    (
        "defi",
        frozenset({"AAVEUSDT", "UNIUSDT", "SUSHIUSDT", "COMPUSDT", "MKRUSDT", "CRVUSDT", "LDOUSDT", "PENDLEUSDT"}),
    ),
    (
        "ai",
        frozenset({"FETUSDT", "AGIXUSDT", "AIUSDT", "WLDUSDT", "TAOUSDT", "RENDERUSDT", "ARKMUSDT"}),
    ),
    (
        "exchange",
        frozenset({"BNBUSDT", "CROUSDT"}),
    ),
    (
        "layer1",
        frozenset({"AVAXUSDT", "DOTUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "SUIUSDT", "SEIUSDT", "INJUSDT"}),
    ),
    (
        "layer2",
        frozenset({"OPUSDT", "ARBUSDT", "STRKUSDT", "MANTAUSDT"}),
    ),
)

RANK_TIE_VALUE_TOLERANCE = 0.25
LIQUIDITY_TIER_PRIORITY = {
    "TIER_A": 3,
    "TIER_B": 2,
    "TIER_C": 1,
}


@dataclass(frozen=True)
class ExecutionTierAssessment:
    tier: str
    quote_volume: float
    spread_bps: float
    liquidity_floor: float


@dataclass(frozen=True)
class MarketStateAssessment:
    market_state: str
    direction: SignalDirection | None
    ema_50_1h: float
    ema_200_1h: float
    ema_50_4h: float
    ema_200_4h: float
    adx_1h: float | None
    ema_slope_norm_1h: float
    bollinger_bandwidth_1h: float | None
    mean_cross_count_1h: int
    volatility_shock: bool
    diagnostics: dict[str, object]


@dataclass(frozen=True)
class AqrrEvaluation:
    outcome: ScanSymbolOutcome
    direction: SignalDirection | None
    candidates: list[SetupCandidate]
    reason_text: str
    filter_reasons: list[str]
    diagnostic: dict[str, object]


@dataclass(frozen=True)
class SelectionDecision:
    selected: list[SetupCandidate]
    rejected: dict[tuple[str, str, float], str]


@dataclass(frozen=True)
class CandidateBuildResult:
    candidate: SetupCandidate | None
    raw_rejection_reasons: tuple[str, ...] = ()
    rejection_stage: str | None = None
    setup_diagnostic: dict[str, object] = field(default_factory=dict)


def _candidate_key(candidate: SetupCandidate) -> tuple[str, str, float]:
    return (candidate.symbol, candidate.direction.value, round(candidate.entry_price, 8))


def _dedupe_reasons(reasons: list[str]) -> list[str]:
    return list(dict.fromkeys(reason for reason in reasons if reason))


def _build_result(
    *,
    candidate: SetupCandidate | None,
    raw_rejection_reasons: list[str] | None = None,
    rejection_stage: str | None = None,
    setup_diagnostic: dict[str, object] | None = None,
) -> CandidateBuildResult:
    reasons = tuple(_dedupe_reasons(list(raw_rejection_reasons or [])))
    diagnostic = dict(setup_diagnostic or {})
    diagnostic["candidate_built"] = candidate is not None
    diagnostic["raw_rejection_reason"] = reasons[0] if reasons else None
    diagnostic["raw_rejection_reasons"] = list(reasons)
    if candidate is not None:
        diagnostic["selected_entry_style"] = candidate.entry_style
    return CandidateBuildResult(
        candidate=candidate,
        raw_rejection_reasons=reasons,
        rejection_stage=rejection_stage,
        setup_diagnostic=diagnostic,
    )


def _coerce_build_result(
    *,
    setup_family: str,
    build_output: CandidateBuildResult | SetupCandidate | None,
    entry_types_considered: list[str],
) -> CandidateBuildResult:
    if isinstance(build_output, CandidateBuildResult):
        return build_output
    setup_diagnostic = {
        "setup_family": setup_family,
        "entry_types_considered": entry_types_considered,
    }
    return _build_result(
        candidate=build_output if isinstance(build_output, SetupCandidate) else None,
        rejection_stage="candidate_build",
        setup_diagnostic=setup_diagnostic,
    )


def _correlation(left: list[float], right: list[float]) -> float:
    size = min(len(left), len(right))
    if size < 3:
        return 0.0
    left_values = left[-size:]
    right_values = right[-size:]
    left_mean = sum(left_values) / size
    right_mean = sum(right_values) / size
    left_diff = [value - left_mean for value in left_values]
    right_diff = [value - right_mean for value in right_values]
    numerator = sum(a * b for a, b in zip(left_diff, right_diff))
    left_scale = sum(a * a for a in left_diff)
    right_scale = sum(b * b for b in right_diff)
    if left_scale <= 0 or right_scale <= 0:
        return 0.0
    return numerator / ((left_scale * right_scale) ** 0.5)


def _cluster_for_symbol(symbol: str) -> str | None:
    normalized_symbol = symbol.upper()
    for cluster_name, symbols in THEME_CLUSTERS:
        if normalized_symbol in symbols:
            return cluster_name
    return None


def _liquidity_tier_priority(candidate: SetupCandidate) -> int:
    return LIQUIDITY_TIER_PRIORITY.get(str(candidate.execution_tier or "").upper(), 0)


def _higher_timeframe_structure_quality(candidate: SetupCandidate) -> float:
    selection_quality = candidate.selection_context.get("higher_timeframe_structure_quality")
    if isinstance(selection_quality, (float, int)):
        return float(selection_quality)
    regime_alignment = candidate.score_breakdown.get("regime_alignment")
    if isinstance(regime_alignment, (float, int)):
        return float(regime_alignment)
    return 0.0


def _correlation_to_selected(candidate: SetupCandidate, *, selected: list[SetupCandidate]) -> float:
    if not selected:
        return 0.0
    candidate_returns = candidate.selection_context.get("returns_1h") or []
    if len(candidate_returns) < 3:
        return 1.0
    max_abs_correlation = 0.0
    for existing in selected:
        existing_returns = existing.selection_context.get("returns_1h") or []
        if len(existing_returns) < 3:
            return 1.0
        max_abs_correlation = max(max_abs_correlation, abs(_correlation(candidate_returns, existing_returns)))
    return max_abs_correlation


def _compare_ranked_candidates(
    left: SetupCandidate,
    right: SetupCandidate,
    *,
    selected: list[SetupCandidate],
) -> int:
    rank_gap = float(left.rank_value) - float(right.rank_value)
    if abs(rank_gap) > RANK_TIE_VALUE_TOLERANCE:
        return -1 if rank_gap > 0 else 1

    net_r_gap = float(left.net_r_multiple) - float(right.net_r_multiple)
    if net_r_gap != 0:
        return -1 if net_r_gap > 0 else 1

    cost_gap = float(left.estimated_cost) - float(right.estimated_cost)
    if cost_gap != 0:
        return -1 if cost_gap < 0 else 1

    left_corr = _correlation_to_selected(left, selected=selected)
    right_corr = _correlation_to_selected(right, selected=selected)
    if left_corr != right_corr:
        return -1 if left_corr < right_corr else 1

    left_liquidity = _liquidity_tier_priority(left)
    right_liquidity = _liquidity_tier_priority(right)
    if left_liquidity != right_liquidity:
        return -1 if left_liquidity > right_liquidity else 1

    left_structure = _higher_timeframe_structure_quality(left)
    right_structure = _higher_timeframe_structure_quality(right)
    if left_structure != right_structure:
        return -1 if left_structure > right_structure else 1

    if left.final_score != right.final_score:
        return -1 if left.final_score > right.final_score else 1
    if left.confirmation_score != right.confirmation_score:
        return -1 if left.confirmation_score > right.confirmation_score else 1
    if left.symbol != right.symbol:
        return -1 if left.symbol < right.symbol else 1
    return 0


def rank_candidates(
    candidates: list[SetupCandidate],
    *,
    selected: list[SetupCandidate] | None = None,
) -> list[SetupCandidate]:
    current_selected = selected or []
    return sorted(
        candidates,
        key=cmp_to_key(
            lambda left, right: _compare_ranked_candidates(
                left,
                right,
                selected=current_selected,
            )
        ),
    )


def _expected_hold_hours(setup_family: str) -> float:
    if setup_family == SETUP_RANGE:
        return 6.0
    if setup_family == SETUP_BREAKOUT:
        return 10.0
    return 12.0


def _execution_tier(
    *,
    quote_volume: float,
    spread_bps: float,
    spread_relative_ratio: float | None = None,
    relative_spread_ready: bool = False,
    liquidity_floor: float,
    config: StrategyConfig,
) -> ExecutionTierAssessment:
    if spread_bps <= float(config.spread_tier_a_bps) and quote_volume >= max(liquidity_floor * 1.5, liquidity_floor):
        tier = "TIER_A"
    elif spread_bps <= float(config.spread_tier_b_bps) and quote_volume >= liquidity_floor:
        tier = "TIER_B"
    else:
        tier = "TIER_C"
    return ExecutionTierAssessment(
        tier=tier,
        quote_volume=quote_volume,
        spread_bps=spread_bps,
        liquidity_floor=liquidity_floor,
    )


def classify_market_state(
    *,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    config: StrategyConfig,
    spread_bps: float | None = None,
    quote_volume: float | None = None,
    liquidity_floor: float | None = None,
    spread_relative_ratio: float | None = None,
    relative_spread_ready: bool = False,
) -> MarketStateAssessment:
    closes_1h = closes(candles_1h)
    closes_4h = closes(candles_4h)
    ema_50_1h_series = ema_series(closes_1h, config.ema_slow_period)
    ema_200_1h_series = ema_series(closes_1h, config.ema_context_period)
    ema_50_4h_series = ema_series(closes_4h, config.ema_slow_period)
    ema_200_4h_series = ema_series(closes_4h, config.ema_context_period)
    ema_50_1h = ema_50_1h_series[-1]
    ema_200_1h = ema_200_1h_series[-1]
    ema_50_4h = ema_50_4h_series[-1]
    ema_200_4h = ema_200_4h_series[-1]
    adx_metrics = calculate_trend_metrics(candles_1h, period=config.atr_period_1h)
    atr_1h = calculate_atr(candles_1h, period=config.atr_period_1h)
    atr_percentile_1h, atr_pct_1h = _atr_percentile_1h(candles_1h, period=config.atr_period_1h)
    ema_slope_norm_1h = normalized_ema_slope(ema_50_1h_series, atr_1h, lookback=10)
    bandwidth_now = bollinger_bandwidth(closes_1h, config.bollinger_period, float(config.bollinger_std_mult))
    historical_bandwidths = historical_bollinger_bandwidths(closes_1h, config.bollinger_period, float(config.bollinger_std_mult))
    bandwidth_threshold = median(historical_bandwidths[-50:]) if historical_bandwidths else None
    mean_crosses = mean_cross_count(closes_1h, ema_50_1h_series, lookback=20)
    volatility_shock, raw_volatility_diag = volatility_shock_flag(
        candles_15m,
        atr_period=config.atr_period_15m,
        range_multiple=float(config.volatility_shock_range_multiple),
    )
    volatility_diag = {
        "atr_pct_15m": raw_volatility_diag.get("atr_pct"),
        "atr_percentile_15m": raw_volatility_diag.get("atr_percentile"),
        "median_range_50": raw_volatility_diag.get("median_range_50"),
        "current_range": raw_volatility_diag.get("current_range"),
    }
    range_contained_1h, range_containment_diag = _range_containment_1h(candles_1h, atr_1h=atr_1h)
    pump_dump_profile, pump_dump_diag = _pump_dump_profile_unstable(candles_15m)

    spread_liquidity_unstable_reasons: list[str] = []
    severe_spread_threshold = max(float(config.max_book_spread_bps) * 1.35, float(config.spread_tier_b_bps) * 1.5)
    if spread_bps is not None and spread_bps >= severe_spread_threshold:
        spread_liquidity_unstable_reasons.append("severe_spread_degradation")
    if (
        relative_spread_ready
        and spread_relative_ratio is not None
        and spread_relative_ratio >= 3.0
    ):
        spread_liquidity_unstable_reasons.append("relative_spread_degradation")
    if (
        quote_volume is not None
        and liquidity_floor is not None
        and liquidity_floor > 0
        and quote_volume < (liquidity_floor * 0.5)
    ):
        spread_liquidity_unstable_reasons.append("liquidity_degradation")
    spread_liquidity_unstable = bool(spread_liquidity_unstable_reasons)

    market_state = MARKET_STATE_UNSTABLE
    direction: SignalDirection | None = None
    if (
        not volatility_shock
        and not pump_dump_profile
        and not spread_liquidity_unstable
        and ema_50_1h > ema_200_1h
        and ema_50_4h >= ema_200_4h
        and (adx_metrics.adx or 0.0) >= config.trend_adx_threshold
        and ema_slope_norm_1h > 0
    ):
        market_state = MARKET_STATE_BULL
        direction = SignalDirection.LONG
    elif (
        not volatility_shock
        and not pump_dump_profile
        and not spread_liquidity_unstable
        and ema_50_1h < ema_200_1h
        and ema_50_4h <= ema_200_4h
        and (adx_metrics.adx or 0.0) >= config.trend_adx_threshold
        and ema_slope_norm_1h < 0
    ):
        market_state = MARKET_STATE_BEAR
        direction = SignalDirection.SHORT
    elif (
        not volatility_shock
        and not pump_dump_profile
        and not spread_liquidity_unstable
        and (adx_metrics.adx or 0.0) <= config.range_adx_threshold
        and bandwidth_now is not None
        and bandwidth_threshold is not None
        and bandwidth_now <= bandwidth_threshold * 1.10
        and mean_crosses >= 4
        and range_contained_1h
    ):
        market_state = MARKET_STATE_RANGE

    return MarketStateAssessment(
        market_state=market_state,
        direction=direction,
        ema_50_1h=ema_50_1h,
        ema_200_1h=ema_200_1h,
        ema_50_4h=ema_50_4h,
        ema_200_4h=ema_200_4h,
        adx_1h=adx_metrics.adx,
        ema_slope_norm_1h=ema_slope_norm_1h,
        bollinger_bandwidth_1h=bandwidth_now,
        mean_cross_count_1h=mean_crosses,
        volatility_shock=volatility_shock,
        diagnostics={
            "ema_50_1h": ema_50_1h,
            "ema_200_1h": ema_200_1h,
            "ema_50_4h": ema_50_4h,
            "ema_200_4h": ema_200_4h,
            "adx_1h": adx_metrics.adx,
            "plus_di_1h": adx_metrics.plus_di,
            "minus_di_1h": adx_metrics.minus_di,
            "atr_1h": atr_1h,
            "atr_pct_1h": atr_pct_1h,
            "atr_percentile": atr_percentile_1h,
            "ema_slope_norm_1h": ema_slope_norm_1h,
            "bollinger_bandwidth_1h": bandwidth_now,
            "bollinger_bandwidth_threshold_1h": bandwidth_threshold,
            "mean_cross_count_1h": mean_crosses,
            "range_contained_1h": range_contained_1h,
            "pump_dump_profile": pump_dump_profile,
            "spread_liquidity_unstable": spread_liquidity_unstable,
            "spread_bps_input": spread_bps,
            "spread_relative_ratio_input": spread_relative_ratio,
            "relative_spread_ready_input": relative_spread_ready,
            "quote_volume_input": quote_volume,
            "liquidity_floor_input": liquidity_floor,
            "spread_liquidity_unstable_reasons": spread_liquidity_unstable_reasons,
            **range_containment_diag,
            **volatility_diag,
            **pump_dump_diag,
        },
    )


def _atr_percentile_1h(candles_1h: list[Candle], *, period: int) -> tuple[float | None, float | None]:
    if len(candles_1h) <= period:
        return None, None
    atr_history_pct: list[float] = []
    close_values = closes(candles_1h)
    for index in range(period + 1, len(candles_1h) + 1):
        atr_value = calculate_atr(candles_1h[:index], period=period)
        close_value = close_values[index - 1]
        if atr_value is None or close_value <= 0:
            continue
        atr_history_pct.append(atr_value / close_value)
    if not atr_history_pct:
        return None, None
    current_atr_pct = atr_history_pct[-1]
    historical_values = atr_history_pct[:-1]
    if not historical_values:
        return 1.0, current_atr_pct
    return percentile_rank(historical_values, current_atr_pct), current_atr_pct


def _range_containment_1h(candles_1h: list[Candle], *, atr_1h: float | None) -> tuple[bool, dict[str, float | bool | None]]:
    if len(candles_1h) < 24:
        return False, {
            "recent_range_high_1h": None,
            "recent_range_low_1h": None,
            "prior_range_high_1h": None,
            "prior_range_low_1h": None,
        }
    recent = candles_1h[-12:]
    prior = candles_1h[-24:-12]
    recent_high = max(candle.high for candle in recent)
    recent_low = min(candle.low for candle in recent)
    prior_high = max(candle.high for candle in prior)
    prior_low = min(candle.low for candle in prior)
    tolerance = (atr_1h or 0.0) * 0.25
    contained = recent_high <= (prior_high + tolerance) and recent_low >= (prior_low - tolerance)
    recent_width = recent_high - recent_low
    prior_width = prior_high - prior_low
    stable = prior_width > 0 and recent_width <= (prior_width + (tolerance * 2))
    return bool(contained and stable), {
        "recent_range_high_1h": recent_high,
        "recent_range_low_1h": recent_low,
        "prior_range_high_1h": prior_high,
        "prior_range_low_1h": prior_low,
        "range_containment_tolerance_1h": tolerance,
    }


def _pump_dump_profile_unstable(candles_15m: list[Candle]) -> tuple[bool, dict[str, float | bool | None]]:
    if len(candles_15m) < 8:
        return False, {
            "pump_dump_move_pct_2h": None,
            "pump_dump_range_burst_ratio": None,
        }

    recent_window = candles_15m[-8:]
    start_price = recent_window[0].open
    end_price = recent_window[-1].close
    if start_price <= 0:
        return False, {
            "pump_dump_move_pct_2h": None,
            "pump_dump_range_burst_ratio": None,
        }

    move_pct_2h = (end_price - start_price) / start_price
    max_recent_range = max(candle.range_size for candle in recent_window)
    baseline_range = recent_median_range(candles_15m, lookback=50)
    range_burst_ratio = (
        max_recent_range / baseline_range
        if baseline_range is not None and baseline_range > 0
        else None
    )
    unstable = (
        abs(move_pct_2h) >= 0.05
        and range_burst_ratio is not None
        and range_burst_ratio >= 2.5
    )
    return unstable, {
        "pump_dump_move_pct_2h": move_pct_2h,
        "pump_dump_range_burst_ratio": range_burst_ratio,
    }


def _estimated_cost_distance(
    *,
    entry_price: float,
    spread_bps: float,
    funding_rate: float,
    next_funding_time_ms: int | None,
    direction: SignalDirection,
    entry_style: str,
    setup_family: str,
    config: StrategyConfig,
    account_maker_fee_rate: float | None = None,
    account_taker_fee_rate: float | None = None,
    funding_rate_history: list[float] | None = None,
    now: datetime | None = None,
) -> float:
    spread_fraction = spread_bps / 10000.0
    maker_fee_rate = float(config.maker_fee_rate)
    taker_fee_rate = float(config.taker_fee_rate)
    if account_maker_fee_rate is not None and account_maker_fee_rate >= 0:
        maker_fee_rate = account_maker_fee_rate
    if account_taker_fee_rate is not None and account_taker_fee_rate >= 0:
        taker_fee_rate = account_taker_fee_rate
    entry_fee_rate = maker_fee_rate if entry_style == "LIMIT_GTD" else taker_fee_rate
    exit_fee_rate = taker_fee_rate
    limit_slippage_floor = float(config.slippage_rate_floor) / 2.0
    exit_slippage_floor = float(config.slippage_rate_floor) / 2.0
    if entry_style == "LIMIT_GTD":
        entry_slippage_fraction = max(limit_slippage_floor, spread_fraction * 0.25)
    else:
        entry_slippage_fraction = max(float(config.slippage_rate_floor), spread_fraction)
    exit_slippage_fraction = max(exit_slippage_floor, spread_fraction * 0.50)

    adverse_funding_rate = 0.0
    if direction == SignalDirection.LONG and funding_rate > 0:
        adverse_funding_rate = abs(funding_rate)
    elif direction == SignalDirection.SHORT and funding_rate < 0:
        adverse_funding_rate = abs(funding_rate)

    if funding_rate_history:
        historical_adverse = [
            abs(rate)
            for rate in funding_rate_history
            if (
                (direction == SignalDirection.LONG and rate > 0)
                or (direction == SignalDirection.SHORT and rate < 0)
            )
        ]
        if historical_adverse:
            historical_adverse.sort()
            percentile_index = max(min(len(historical_adverse) - 1, ceil(len(historical_adverse) * 0.75) - 1), 0)
            adverse_funding_rate = max(adverse_funding_rate, historical_adverse[percentile_index])

    adverse_funding_events = 0
    adverse_funding = 0.0
    expected_hold_seconds = _expected_hold_hours(setup_family) * 3600.0
    funding_interval_seconds = 8 * 3600.0
    if adverse_funding_rate > 0 and next_funding_time_ms:
        now_utc = now or datetime.now(timezone.utc)
        seconds_until_next_funding = max((next_funding_time_ms / 1000) - now_utc.timestamp(), 0.0)
        if expected_hold_seconds > seconds_until_next_funding:
            extra_horizon = expected_hold_seconds - seconds_until_next_funding
            adverse_funding_events = 1 + max(int(extra_horizon // funding_interval_seconds), 0)
    elif adverse_funding_rate > 0 and next_funding_time_ms is None:
        adverse_funding_events = max(1, ceil(expected_hold_seconds / funding_interval_seconds))
    if adverse_funding_events > 0:
        adverse_funding = adverse_funding_rate * adverse_funding_events

    total_fraction = (
        entry_fee_rate
        + exit_fee_rate
        + entry_slippage_fraction
        + exit_slippage_fraction
        + adverse_funding
    )
    return entry_price * total_fraction


def _required_reward_distance(*, risk_distance: float, estimated_cost: float, min_net_r_multiple: float) -> float:
    return (min_net_r_multiple * (risk_distance + estimated_cost)) + estimated_cost


def _net_r_multiple(*, reward_distance: float, risk_distance: float, estimated_cost: float) -> float:
    if risk_distance <= 0:
        return 0.0
    net_reward = reward_distance - estimated_cost
    net_risk = risk_distance + estimated_cost
    if net_reward <= 0 or net_risk <= 0:
        return 0.0
    return net_reward / net_risk


def _required_leverage(
    *,
    entry_price: float,
    stop_loss: float,
    available_balance: float,
    filters_min_notional: float,
    config: StrategyConfig,
    remaining_entry_slots: int | None = None,
    remaining_portfolio_risk_usdt: float | None = None,
) -> tuple[float, int]:
    risk_distance_pct = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0.0
    if risk_distance_pct <= 0 or available_balance <= 0:
        return 0.0, config.max_leverage + 1
    raw_remaining_slots = config.max_entry_ideas if remaining_entry_slots is None else remaining_entry_slots
    effective_remaining_slots = max(raw_remaining_slots, 1)
    effective_remaining_portfolio_risk = (
        max(remaining_portfolio_risk_usdt, 0.0)
        if remaining_portfolio_risk_usdt is not None
        else available_balance * float(config.max_portfolio_risk_fraction)
    )
    if effective_remaining_portfolio_risk <= 0:
        return 0.0, config.max_leverage + 1
    risk_usd = min(
        available_balance * float(config.risk_per_trade_fraction),
        effective_remaining_portfolio_risk / effective_remaining_slots,
    )
    raw_notional = risk_usd / risk_distance_pct if risk_distance_pct > 0 else 0.0
    planned_notional = max(raw_notional, filters_min_notional * 1.05)
    margin_per_trade = (available_balance * float(config.deployable_equity_fraction)) / effective_remaining_slots
    required_leverage = ceil(planned_notional / margin_per_trade) if margin_per_trade > 0 else config.max_leverage + 1
    return planned_notional, required_leverage


def _reward_headroom_barrier(
    *,
    direction: SignalDirection,
    entry_price: float,
    candles_1h: list[Candle],
    fallback_r_multiple: float,
    risk_distance: float,
) -> float:
    if direction == SignalDirection.LONG:
        barriers = [candle.high for candle in candles_1h[-24:] if candle.high > entry_price]
        return min(barriers) if barriers else entry_price + (risk_distance * fallback_r_multiple)
    barriers = [candle.low for candle in candles_1h[-24:] if candle.low < entry_price]
    return max(barriers) if barriers else entry_price - (risk_distance * fallback_r_multiple)


def _weighted_score(component_scores: dict[str, float]) -> tuple[int, dict[str, int]]:
    weights = {
        "structure_quality": 25,
        "regime_alignment": 20,
        "confirmation_quality": 15,
        "liquidity_execution_quality": 15,
        "volatility_quality": 10,
        "reward_headroom_quality": 10,
        "funding_carry_quality": 5,
    }
    breakdown = {
        key: int(round(weights[key] * max(min(value, 1.0), 0.0)))
        for key, value in component_scores.items()
    }
    return sum(breakdown.values()), breakdown


def _confirmation_score(component_scores: dict[str, float]) -> int:
    relevant = [
        component_scores["structure_quality"],
        component_scores["regime_alignment"],
        component_scores["confirmation_quality"],
        component_scores["reward_headroom_quality"],
    ]
    return int(round((sum(relevant) / len(relevant)) * 100))


def _score_candidate(
    *,
    structure_quality: float,
    regime_alignment: float,
    confirmation_quality: float,
    liquidity_execution_quality: float,
    volatility_quality: float,
    reward_headroom_quality: float,
    funding_carry_quality: float,
) -> tuple[int, int, dict[str, int]]:
    component_scores = {
        "structure_quality": structure_quality,
        "regime_alignment": regime_alignment,
        "confirmation_quality": confirmation_quality,
        "liquidity_execution_quality": liquidity_execution_quality,
        "volatility_quality": volatility_quality,
        "reward_headroom_quality": reward_headroom_quality,
        "funding_carry_quality": funding_carry_quality,
    }
    final_score, breakdown = _weighted_score(component_scores)
    return final_score, _confirmation_score(component_scores), breakdown


def _bullish_engulf(signal_bar: Candle, prior_bar: Candle) -> bool:
    return (
        signal_bar.close > signal_bar.open
        and prior_bar.close < prior_bar.open
        and signal_bar.open <= prior_bar.close
        and signal_bar.close >= prior_bar.open
    )


def _bearish_engulf(signal_bar: Candle, prior_bar: Candle) -> bool:
    return (
        signal_bar.close < signal_bar.open
        and prior_bar.close > prior_bar.open
        and signal_bar.open >= prior_bar.close
        and signal_bar.close <= prior_bar.open
    )


def _long_higher_low_recovery(candles_15m: list[Candle]) -> bool:
    if len(candles_15m) < 4:
        return False
    prior_countertrend = candles_15m[-2]
    prior_pivot = candles_15m[-3]
    signal_bar = candles_15m[-1]
    return (
        prior_countertrend.low <= prior_pivot.low
        and signal_bar.low > prior_countertrend.low
        and signal_bar.close >= prior_countertrend.close
    )


def _short_lower_high_recovery(candles_15m: list[Candle]) -> bool:
    if len(candles_15m) < 4:
        return False
    prior_countertrend = candles_15m[-2]
    prior_pivot = candles_15m[-3]
    signal_bar = candles_15m[-1]
    return (
        prior_countertrend.high >= prior_pivot.high
        and signal_bar.high < prior_countertrend.high
        and signal_bar.close <= prior_countertrend.close
    )


def _countertrend_momentum_loss(*, candles_15m: list[Candle], direction: SignalDirection) -> bool:
    if len(candles_15m) < 4:
        return False
    prior_bar = candles_15m[-2]
    earlier_bar = candles_15m[-3]
    signal_bar = candles_15m[-1]
    if direction == SignalDirection.LONG:
        prior_body = max(prior_bar.open - prior_bar.close, 0.0)
        earlier_body = max(earlier_bar.open - earlier_bar.close, 0.0)
        return (
            prior_bar.close < prior_bar.open
            and earlier_bar.close < earlier_bar.open
            and prior_body <= earlier_body * 0.85
            and signal_bar.close >= signal_bar.open
        )
    prior_body = max(prior_bar.close - prior_bar.open, 0.0)
    earlier_body = max(earlier_bar.close - earlier_bar.open, 0.0)
    return (
        prior_bar.close > prior_bar.open
        and earlier_bar.close > earlier_bar.open
        and prior_body <= earlier_body * 0.85
        and signal_bar.close <= signal_bar.open
    )


def _candidate_core_threshold_failures(candidate: SetupCandidate, config: StrategyConfig) -> tuple[list[str], dict[str, object]]:
    raw_rejection_reasons: list[str] = []
    diagnostic: dict[str, object] = {
        "net_r_multiple": round(candidate.net_r_multiple, 4),
        "tier_threshold": config.tier_a_min_score if candidate.execution_tier == "TIER_A" else config.tier_b_min_score,
    }
    if candidate.net_r_multiple < float(config.min_net_r_multiple):
        raw_rejection_reasons.append("net_r_multiple_below_min")

    core_keys = {
        "structure_quality",
        "regime_alignment",
        "liquidity_execution_quality",
        "reward_headroom_quality",
    }
    max_weights = {
        "structure_quality": 25,
        "regime_alignment": 20,
        "liquidity_execution_quality": 15,
        "reward_headroom_quality": 10,
    }
    core_components_below_min = [
        key
        for key in core_keys
        if (candidate.score_breakdown.get(key, 0) / max_weights[key]) < 0.5
    ]
    diagnostic["core_components_below_min"] = core_components_below_min
    if core_components_below_min:
        raw_rejection_reasons.append("core_component_below_min")

    if candidate.final_score < diagnostic["tier_threshold"]:
        raw_rejection_reasons.append("tier_score_below_min")

    return _dedupe_reasons(raw_rejection_reasons), diagnostic


def _build_breakout_candidate(
    *,
    symbol: str,
    direction: SignalDirection,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    current_price: float,
    funding_rate: float,
    next_funding_time_ms: int | None = None,
    account_maker_fee_rate: float | None = None,
    account_taker_fee_rate: float | None = None,
    funding_rate_history: list[float] | None = None,
    execution_tier: ExecutionTierAssessment,
    market_state: MarketStateAssessment,
    config: StrategyConfig,
    tick_size: float,
    with_diagnostics: bool = False,
) -> SetupCandidate | CandidateBuildResult | None:
    atr_15m = calculate_atr(candles_15m, period=config.atr_period_15m)
    setup_diagnostic: dict[str, object] = {
        "setup_family": SETUP_BREAKOUT,
        "entry_types_considered": ["LIMIT_GTD", "STOP_ENTRY"],
    }
    if atr_15m is None or atr_15m <= 0 or len(candles_15m) < config.breakout_lookback_bars + 2:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["breakout_insufficient_history"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    signal_bar = candles_15m[-1]
    prior_window = candles_15m[-(config.breakout_lookback_bars + 1) : -1]
    breakout_level = max(candle.high for candle in prior_window) if direction == SignalDirection.LONG else min(candle.low for candle in prior_window)
    breakout_range = signal_bar.range_size
    breakout_body = body_fraction(signal_bar)
    breakout_volume = volume_ratio(candles_15m, lookback=20)
    setup_diagnostic.update(
        {
            "breakout_level": breakout_level,
            "breakout_range": breakout_range,
            "breakout_body_fraction": round(breakout_body, 4),
            "breakout_volume_ratio": round(breakout_volume, 4),
            "atr_15m": atr_15m,
        }
    )
    if direction == SignalDirection.LONG:
        if signal_bar.close <= breakout_level:
            result = _build_result(
                candidate=None,
                raw_rejection_reasons=["breakout_not_closed_through_level"],
                rejection_stage="candidate_build",
                setup_diagnostic=setup_diagnostic,
            )
            return result if with_diagnostics else result.candidate
        if signal_bar.close - breakout_level > atr_15m * 0.6:
            result = _build_result(
                candidate=None,
                raw_rejection_reasons=["breakout_too_extended"],
                rejection_stage="candidate_build",
                setup_diagnostic=setup_diagnostic,
            )
            return result if with_diagnostics else result.candidate
    else:
        if signal_bar.close >= breakout_level:
            result = _build_result(
                candidate=None,
                raw_rejection_reasons=["breakout_not_closed_through_level"],
                rejection_stage="candidate_build",
                setup_diagnostic=setup_diagnostic,
            )
            return result if with_diagnostics else result.candidate
        if breakout_level - signal_bar.close > atr_15m * 0.6:
            result = _build_result(
                candidate=None,
                raw_rejection_reasons=["breakout_too_extended"],
                rejection_stage="candidate_build",
                setup_diagnostic=setup_diagnostic,
            )
            return result if with_diagnostics else result.candidate
    if not (
        breakout_volume >= float(config.breakout_volume_ratio_min)
        or (
            breakout_body >= float(config.breakout_body_fraction_min)
            and breakout_range <= atr_15m * float(config.breakout_range_atr_cap)
        )
    ):
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["breakout_participation_filter_failed"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate

    stop_buffer = max(
        atr_15m * float(config.stop_buffer_atr_fraction),
        tick_size * 3,
        current_price * ((execution_tier.spread_bps / 10000.0) * 2),
    )
    entry_style = "LIMIT_GTD"
    zone_span = atr_15m * float(config.breakout_entry_zone_atr_fraction)
    if direction == SignalDirection.LONG:
        retest_zone_low = breakout_level
        retest_zone_high = breakout_level + zone_span
        entry_price = retest_zone_low + (zone_span * 0.5)
    else:
        retest_zone_high = breakout_level
        retest_zone_low = breakout_level - zone_span
        entry_price = retest_zone_high - (zone_span * 0.5)
    if breakout_volume >= 1.5 and breakout_body >= 0.70:
        entry_style = "STOP_ENTRY"
        entry_price = signal_bar.high + tick_size if direction == SignalDirection.LONG else signal_bar.low - tick_size
    recent_swing = min(candle.low for candle in candles_15m[-4:]) if direction == SignalDirection.LONG else max(candle.high for candle in candles_15m[-4:])
    stop_loss = min(recent_swing, breakout_level) - stop_buffer if direction == SignalDirection.LONG else max(recent_swing, breakout_level) + stop_buffer
    risk_distance = abs(entry_price - stop_loss)
    if risk_distance <= 0:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["breakout_invalid_risk_distance"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    estimated_cost = _estimated_cost_distance(
        entry_price=entry_price,
        spread_bps=execution_tier.spread_bps,
        funding_rate=funding_rate,
        next_funding_time_ms=next_funding_time_ms,
        direction=direction,
        entry_style=entry_style,
        setup_family=SETUP_BREAKOUT,
        config=config,
        account_maker_fee_rate=account_maker_fee_rate,
        account_taker_fee_rate=account_taker_fee_rate,
        funding_rate_history=funding_rate_history,
    )
    barrier_price = _reward_headroom_barrier(
        direction=direction,
        entry_price=entry_price,
        candles_1h=candles_1h,
        fallback_r_multiple=6.0,
        risk_distance=risk_distance,
    )
    available_reward = barrier_price - entry_price if direction == SignalDirection.LONG else entry_price - barrier_price
    required_reward = _required_reward_distance(
        risk_distance=risk_distance,
        estimated_cost=estimated_cost,
        min_net_r_multiple=float(config.min_net_r_multiple),
    )
    if available_reward < required_reward:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["breakout_net_3r_headroom_failed"],
            rejection_stage="candidate_build",
            setup_diagnostic={
                **setup_diagnostic,
                "available_reward": round(available_reward, 8),
                "required_reward": round(required_reward, 8),
            },
        )
        return result if with_diagnostics else result.candidate
    take_profit = entry_price + required_reward if direction == SignalDirection.LONG else entry_price - required_reward
    net_r_multiple = _net_r_multiple(
        reward_distance=required_reward,
        risk_distance=risk_distance,
        estimated_cost=estimated_cost,
    )
    structure_quality = 1.0 if breakout_volume >= 1.4 and breakout_body >= 0.70 else 0.85
    regime_alignment = 1.0 if market_state.direction == direction else 0.55
    confirmation_quality = min(1.0, max(0.6, (breakout_volume - 0.6) / 1.0))
    liquidity_execution_quality = 1.0 if execution_tier.tier == "TIER_A" else 0.80
    volatility_quality = 0.80 if market_state.volatility_shock else 1.0
    reward_headroom_quality = min(1.0, max(0.6, available_reward / max(required_reward, 1e-9)))
    funding_carry_quality = 1.0 if estimated_cost <= entry_price * 0.002 else 0.70
    final_score, confirmation_score, score_breakdown = _score_candidate(
        structure_quality=structure_quality,
        regime_alignment=regime_alignment,
        confirmation_quality=confirmation_quality,
        liquidity_execution_quality=liquidity_execution_quality,
        volatility_quality=volatility_quality,
        reward_headroom_quality=reward_headroom_quality,
        funding_carry_quality=funding_carry_quality,
    )
    candidate = SetupCandidate(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        actual_rr=required_reward / risk_distance,
        net_r_multiple=net_r_multiple,
        estimated_cost=estimated_cost,
        confirmation_score=confirmation_score,
        final_score=final_score,
        rank_value=float(final_score),
        setup_family=SETUP_BREAKOUT,
        setup_variant="trend_breakout_retest",
        entry_style=entry_style,
        market_state=market_state.market_state,
        execution_tier=execution_tier.tier,
        score_breakdown=score_breakdown,
        reason_text="trend breakout retest candidate",
        current_price=current_price,
        swing_origin=recent_swing,
        swing_terminus=breakout_level,
        expiry_minutes=config.breakout_retest_expiry_bars * 15,
        extra_context={
            "aqrr_setup_diagnostics": {
                SETUP_BREAKOUT: {
                    **setup_diagnostic,
                    "selected_entry_style": entry_style,
                    "available_reward": round(available_reward, 8),
                    "required_reward": round(required_reward, 8),
                    "risk_distance": round(risk_distance, 8),
                }
            },
            "strategy_context": {
                "breakout_level": breakout_level,
                "retest_entry_zone_low": retest_zone_low,
                "retest_entry_zone_high": retest_zone_high,
                "breakout_signal_bar_high": signal_bar.high,
                "breakout_signal_bar_low": signal_bar.low,
                "breakout_failure_level": breakout_level,
                "expiry_bars": config.breakout_retest_expiry_bars,
                "cancellation_triggers": [
                    "breakout_failure_back_inside_range",
                    "regime_flip",
                    "spread_deterioration",
                    "volatility_shock",
                    "invalidation_structure_break",
                    "score_viability_loss",
                ],
            }
        },
        selection_context={},
    )
    result = _build_result(
        candidate=candidate,
        rejection_stage=None,
        setup_diagnostic={
            **setup_diagnostic,
            "selected_entry_style": entry_style,
            "available_reward": round(available_reward, 8),
            "required_reward": round(required_reward, 8),
            "risk_distance": round(risk_distance, 8),
        },
    )
    return result if with_diagnostics else result.candidate


def _build_pullback_candidate(
    *,
    symbol: str,
    direction: SignalDirection,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    current_price: float,
    funding_rate: float,
    next_funding_time_ms: int | None = None,
    account_maker_fee_rate: float | None = None,
    account_taker_fee_rate: float | None = None,
    funding_rate_history: list[float] | None = None,
    execution_tier: ExecutionTierAssessment,
    market_state: MarketStateAssessment,
    config: StrategyConfig,
    tick_size: float,
    with_diagnostics: bool = False,
) -> SetupCandidate | CandidateBuildResult | None:
    atr_15m = calculate_atr(candles_15m, period=config.atr_period_15m)
    setup_diagnostic: dict[str, object] = {
        "setup_family": SETUP_PULLBACK,
        "entry_types_considered": ["LIMIT_GTD", "STOP_ENTRY"],
    }
    if atr_15m is None or atr_15m <= 0 or len(candles_15m) < config.ema_slow_period + 5:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["pullback_insufficient_history"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    closes_15m = closes(candles_15m)
    ema_20_series = ema_series(closes_15m, config.ema_fast_period)
    ema_50_series = ema_series(closes_15m, config.ema_slow_period)
    zone_top = max(ema_20_series[-1], ema_50_series[-1])
    zone_bottom = min(ema_20_series[-1], ema_50_series[-1])
    signal_bar = candles_15m[-1]
    prior_bar = candles_15m[-2]
    prior_close = closes(candles_1h)[-1]
    in_zone = signal_bar.low <= zone_top and signal_bar.high >= zone_bottom
    higher_timeframe_trend_valid = (
        prior_close >= market_state.ema_50_1h
        if direction == SignalDirection.LONG
        else prior_close <= market_state.ema_50_1h
    )
    wick_rejection = (
        lower_shadow_fraction(signal_bar) >= 0.35
        and signal_bar.close >= zone_bottom
        and signal_bar.close >= signal_bar.open
        if direction == SignalDirection.LONG
        else upper_shadow_fraction(signal_bar) >= 0.35
        and signal_bar.close <= zone_top
        and signal_bar.close <= signal_bar.open
    )
    engulf_or_reclaim = (
        _bullish_engulf(signal_bar, prior_bar)
        or (
            signal_bar.close > signal_bar.open
            and signal_bar.close >= ema_20_series[-1]
            and body_fraction(signal_bar) >= float(config.pullback_confirmation_body_fraction_min)
        )
        if direction == SignalDirection.LONG
        else _bearish_engulf(signal_bar, prior_bar)
        or (
            signal_bar.close < signal_bar.open
            and signal_bar.close <= ema_20_series[-1]
            and body_fraction(signal_bar) >= float(config.pullback_confirmation_body_fraction_min)
        )
    )
    local_structure_recovery = (
        _long_higher_low_recovery(candles_15m)
        if direction == SignalDirection.LONG
        else _short_lower_high_recovery(candles_15m)
    )
    countertrend_momentum_loss = _countertrend_momentum_loss(
        candles_15m=candles_15m,
        direction=direction,
    )
    rejection_evidence = {
        "wick_rejection": wick_rejection,
        "engulf_or_reclaim": engulf_or_reclaim,
        "local_structure_recovery": local_structure_recovery,
        "countertrend_momentum_loss": countertrend_momentum_loss,
    }
    setup_diagnostic.update(
        {
            "pullback_zone_top": zone_top,
            "pullback_zone_bottom": zone_bottom,
            "higher_timeframe_close_1h": prior_close,
            "higher_timeframe_trend_valid": higher_timeframe_trend_valid,
            "rejection_evidence": rejection_evidence,
        }
    )
    if not in_zone:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["pullback_zone_not_touched"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    if not higher_timeframe_trend_valid:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["pullback_higher_timeframe_trend_invalid"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    if not any(rejection_evidence.values()):
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["pullback_no_rejection_evidence"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    swing_point = (
        min(candle.low for candle in candles_15m[-5:])
        if direction == SignalDirection.LONG
        else max(candle.high for candle in candles_15m[-5:])
    )
    entry_style = "LIMIT_GTD"
    entry_price = zone_bottom if direction == SignalDirection.LONG else zone_top
    exceptional_rejection = (
        engulf_or_reclaim
        and body_fraction(signal_bar) >= float(config.pullback_confirmation_body_fraction_min)
        and execution_tier.spread_bps <= float(config.spread_tier_a_bps)
    )
    if exceptional_rejection and abs(signal_bar.close - prior_bar.close) >= tick_size * 3:
        entry_style = "STOP_ENTRY"
        entry_price = signal_bar.high + tick_size if direction == SignalDirection.LONG else signal_bar.low - tick_size
    stop_buffer = max(
        atr_15m * float(config.stop_buffer_atr_fraction),
        tick_size * 3,
        current_price * ((execution_tier.spread_bps / 10000.0) * 2),
    )
    stop_loss = swing_point - stop_buffer if direction == SignalDirection.LONG else swing_point + stop_buffer
    risk_distance = abs(entry_price - stop_loss)
    if risk_distance <= 0:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["pullback_invalid_risk_distance"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    estimated_cost = _estimated_cost_distance(
        entry_price=entry_price,
        spread_bps=execution_tier.spread_bps,
        funding_rate=funding_rate,
        next_funding_time_ms=next_funding_time_ms,
        direction=direction,
        entry_style=entry_style,
        setup_family=SETUP_PULLBACK,
        config=config,
        account_maker_fee_rate=account_maker_fee_rate,
        account_taker_fee_rate=account_taker_fee_rate,
        funding_rate_history=funding_rate_history,
    )
    barrier_price = _reward_headroom_barrier(
        direction=direction,
        entry_price=entry_price,
        candles_1h=candles_1h,
        fallback_r_multiple=6.0,
        risk_distance=risk_distance,
    )
    available_reward = barrier_price - entry_price if direction == SignalDirection.LONG else entry_price - barrier_price
    required_reward = _required_reward_distance(
        risk_distance=risk_distance,
        estimated_cost=estimated_cost,
        min_net_r_multiple=float(config.min_net_r_multiple),
    )
    if available_reward < required_reward:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["pullback_net_3r_headroom_failed"],
            rejection_stage="candidate_build",
            setup_diagnostic={
                **setup_diagnostic,
                "available_reward": round(available_reward, 8),
                "required_reward": round(required_reward, 8),
            },
        )
        return result if with_diagnostics else result.candidate
    take_profit = entry_price + required_reward if direction == SignalDirection.LONG else entry_price - required_reward
    net_r_multiple = _net_r_multiple(
        reward_distance=required_reward,
        risk_distance=risk_distance,
        estimated_cost=estimated_cost,
    )
    structure_quality = 0.85 if entry_style == "LIMIT_GTD" else 0.80
    regime_alignment = 1.0 if market_state.direction == direction else 0.55
    confirmation_quality = 0.85 if exceptional_rejection else 0.80 if any(rejection_evidence.values()) else 0.65
    liquidity_execution_quality = 1.0 if execution_tier.tier == "TIER_A" else 0.80
    volatility_quality = 0.80 if market_state.volatility_shock else 0.90
    reward_headroom_quality = min(1.0, max(0.6, available_reward / max(required_reward, 1e-9)))
    funding_carry_quality = 1.0 if estimated_cost <= entry_price * 0.002 else 0.70
    final_score, confirmation_score, score_breakdown = _score_candidate(
        structure_quality=structure_quality,
        regime_alignment=regime_alignment,
        confirmation_quality=confirmation_quality,
        liquidity_execution_quality=liquidity_execution_quality,
        volatility_quality=volatility_quality,
        reward_headroom_quality=reward_headroom_quality,
        funding_carry_quality=funding_carry_quality,
    )
    candidate = SetupCandidate(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        actual_rr=required_reward / risk_distance,
        net_r_multiple=net_r_multiple,
        estimated_cost=estimated_cost,
        confirmation_score=confirmation_score,
        final_score=final_score,
        rank_value=float(final_score),
        setup_family=SETUP_PULLBACK,
        setup_variant="trend_pullback_continuation",
        entry_style=entry_style,
        market_state=market_state.market_state,
        execution_tier=execution_tier.tier,
        score_breakdown=score_breakdown,
        reason_text="trend pullback continuation candidate",
        current_price=current_price,
        swing_origin=swing_point,
        swing_terminus=entry_price,
        expiry_minutes=config.pullback_expiry_bars * 15,
        extra_context={
            "aqrr_setup_diagnostics": {
                SETUP_PULLBACK: {
                    **setup_diagnostic,
                    "selected_entry_style": entry_style,
                    "available_reward": round(available_reward, 8),
                    "required_reward": round(required_reward, 8),
                    "risk_distance": round(risk_distance, 8),
                }
            },
            "strategy_context": {
                "pullback_zone_top": zone_top,
                "pullback_zone_bottom": zone_bottom,
                "pullback_swing_point": swing_point,
                "rejection_evidence": rejection_evidence,
                "expiry_bars": config.pullback_expiry_bars,
                "cancellation_triggers": [
                    "regime_flip",
                    "spread_deterioration",
                    "volatility_shock",
                    "support_or_resistance_break",
                    "score_viability_loss",
                ],
            }
        },
        selection_context={},
    )
    result = _build_result(
        candidate=candidate,
        setup_diagnostic={
            **setup_diagnostic,
            "selected_entry_style": entry_style,
            "available_reward": round(available_reward, 8),
            "required_reward": round(required_reward, 8),
            "risk_distance": round(risk_distance, 8),
        },
    )
    return result if with_diagnostics else result.candidate


def _build_range_candidate(
    *,
    symbol: str,
    candles_15m: list[Candle],
    current_price: float,
    funding_rate: float,
    next_funding_time_ms: int | None = None,
    account_maker_fee_rate: float | None = None,
    account_taker_fee_rate: float | None = None,
    funding_rate_history: list[float] | None = None,
    execution_tier: ExecutionTierAssessment,
    market_state: MarketStateAssessment,
    config: StrategyConfig,
    tick_size: float,
    with_diagnostics: bool = False,
) -> SetupCandidate | CandidateBuildResult | None:
    atr_15m = calculate_atr(candles_15m, period=config.atr_period_15m)
    setup_diagnostic: dict[str, object] = {
        "setup_family": SETUP_RANGE,
        "entry_types_considered": ["LIMIT_GTD"],
    }
    if atr_15m is None or atr_15m <= 0 or len(candles_15m) < config.range_lookback_bars:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["range_insufficient_history"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    box = candles_15m[-config.range_lookback_bars :]
    range_high = max(candle.high for candle in box)
    range_low = min(candle.low for candle in box)
    width = range_high - range_low
    if width <= 0:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["range_width_invalid"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    signal_bar = candles_15m[-1]
    close_values = closes(candles_15m)
    rsi_value = rsi(close_values, period=14)
    touch_fraction = width * float(config.range_touch_fraction)
    direction: SignalDirection | None = None
    if signal_bar.low <= range_low + touch_fraction and signal_bar.close > signal_bar.open and (rsi_value is None or rsi_value <= 45):
        direction = SignalDirection.LONG
        entry_price = range_low + min(touch_fraction * 0.5, atr_15m * 0.15)
        stop_loss = range_low - max(atr_15m * float(config.stop_buffer_atr_fraction), tick_size * 3)
        barrier_price = range_high
    elif signal_bar.high >= range_high - touch_fraction and signal_bar.close < signal_bar.open and (rsi_value is None or rsi_value >= 55):
        direction = SignalDirection.SHORT
        entry_price = range_high - min(touch_fraction * 0.5, atr_15m * 0.15)
        stop_loss = range_high + max(atr_15m * float(config.stop_buffer_atr_fraction), tick_size * 3)
        barrier_price = range_low
    else:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["range_no_reversion_signal"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    risk_distance = abs(entry_price - stop_loss)
    if risk_distance <= 0:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["range_invalid_risk_distance"],
            rejection_stage="candidate_build",
            setup_diagnostic=setup_diagnostic,
        )
        return result if with_diagnostics else result.candidate
    estimated_cost = _estimated_cost_distance(
        entry_price=entry_price,
        spread_bps=execution_tier.spread_bps,
        funding_rate=funding_rate,
        next_funding_time_ms=next_funding_time_ms,
        direction=direction,
        entry_style="LIMIT_GTD",
        setup_family=SETUP_RANGE,
        config=config,
        account_maker_fee_rate=account_maker_fee_rate,
        account_taker_fee_rate=account_taker_fee_rate,
        funding_rate_history=funding_rate_history,
    )
    available_reward = barrier_price - entry_price if direction == SignalDirection.LONG else entry_price - barrier_price
    required_reward = _required_reward_distance(
        risk_distance=risk_distance,
        estimated_cost=estimated_cost,
        min_net_r_multiple=float(config.min_net_r_multiple),
    )
    if available_reward < required_reward:
        result = _build_result(
            candidate=None,
            raw_rejection_reasons=["range_net_3r_headroom_failed"],
            rejection_stage="candidate_build",
            setup_diagnostic={
                **setup_diagnostic,
                "available_reward": round(available_reward, 8),
                "required_reward": round(required_reward, 8),
            },
        )
        return result if with_diagnostics else result.candidate
    take_profit = entry_price + required_reward if direction == SignalDirection.LONG else entry_price - required_reward
    net_r_multiple = _net_r_multiple(
        reward_distance=required_reward,
        risk_distance=risk_distance,
        estimated_cost=estimated_cost,
    )
    structure_quality = 0.80
    regime_alignment = 1.0
    confirmation_quality = 0.75
    liquidity_execution_quality = 1.0 if execution_tier.tier == "TIER_A" else 0.80
    volatility_quality = 0.85
    reward_headroom_quality = min(1.0, max(0.6, available_reward / max(required_reward, 1e-9)))
    funding_carry_quality = 0.85
    final_score, confirmation_score, score_breakdown = _score_candidate(
        structure_quality=structure_quality,
        regime_alignment=regime_alignment,
        confirmation_quality=confirmation_quality,
        liquidity_execution_quality=liquidity_execution_quality,
        volatility_quality=volatility_quality,
        reward_headroom_quality=reward_headroom_quality,
        funding_carry_quality=funding_carry_quality,
    )
    candidate = SetupCandidate(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        actual_rr=required_reward / risk_distance,
        net_r_multiple=net_r_multiple,
        estimated_cost=estimated_cost,
        confirmation_score=confirmation_score,
        final_score=final_score,
        rank_value=float(final_score),
        setup_family=SETUP_RANGE,
        setup_variant="balanced_range_reversion",
        entry_style="LIMIT_GTD",
        market_state=market_state.market_state,
        execution_tier=execution_tier.tier,
        score_breakdown=score_breakdown,
        reason_text="balanced range reversion candidate",
        current_price=current_price,
        swing_origin=range_low,
        swing_terminus=range_high,
        expiry_minutes=config.range_expiry_bars * 15,
        extra_context={
            "aqrr_setup_diagnostics": {
                SETUP_RANGE: {
                    **setup_diagnostic,
                    "selected_entry_style": "LIMIT_GTD",
                    "available_reward": round(available_reward, 8),
                    "required_reward": round(required_reward, 8),
                    "risk_distance": round(risk_distance, 8),
                }
            },
            "strategy_context": {
                "range_low": range_low,
                "range_high": range_high,
                "range_width": width,
                "expiry_bars": config.range_expiry_bars,
                "cancellation_triggers": [
                    "regime_flip",
                    "spread_deterioration",
                    "volatility_shock",
                    "range_structure_break",
                    "score_viability_loss",
                ],
            }
        },
        selection_context={},
    )
    result = _build_result(
        candidate=candidate,
        setup_diagnostic={
            **setup_diagnostic,
            "selected_entry_style": "LIMIT_GTD",
            "available_reward": round(available_reward, 8),
            "required_reward": round(required_reward, 8),
            "risk_distance": round(risk_distance, 8),
        },
    )
    return result if with_diagnostics else result.candidate


def _candidate_passes_core_thresholds(candidate: SetupCandidate, config: StrategyConfig) -> bool:
    raw_rejection_reasons, _diagnostic = _candidate_core_threshold_failures(candidate, config)
    return not raw_rejection_reasons


def evaluate_symbol(
    *,
    symbol: str,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    current_price: float,
    funding_rate: float,
    quote_volume: float,
    spread_bps: float,
    spread_relative_ratio: float | None = None,
    relative_spread_ready: bool = False,
    liquidity_floor: float,
    filters_min_notional: float,
    tick_size: float,
    available_balance: float,
    config: StrategyConfig,
    btc_returns_1h: list[float] | None = None,
    next_funding_time_ms: int | None = None,
    account_maker_fee_rate: float | None = None,
    account_taker_fee_rate: float | None = None,
    funding_rate_history: list[float] | None = None,
    remaining_entry_slots: int | None = None,
    remaining_portfolio_risk_usdt: float | None = None,
) -> AqrrEvaluation:
    execution_tier = _execution_tier(
        quote_volume=quote_volume,
        spread_bps=spread_bps,
        liquidity_floor=liquidity_floor,
        config=config,
    )
    market_state = classify_market_state(
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        config=config,
        spread_bps=spread_bps,
        quote_volume=quote_volume,
        liquidity_floor=liquidity_floor,
        spread_relative_ratio=spread_relative_ratio,
        relative_spread_ready=relative_spread_ready,
    )
    diagnostic: dict[str, object] = {
        "market_state": market_state.market_state,
        "execution_tier": execution_tier.tier,
        "quote_volume": quote_volume,
        "spread_bps": spread_bps,
        "spread_relative_ratio": spread_relative_ratio,
        "relative_spread_ready": relative_spread_ready,
        "liquidity_floor": liquidity_floor,
        "account_maker_fee_rate": account_maker_fee_rate,
        "account_taker_fee_rate": account_taker_fee_rate,
        "funding_rate_history_points": len(funding_rate_history or []),
        "remaining_entry_slots": remaining_entry_slots,
        "remaining_portfolio_risk_usdt": remaining_portfolio_risk_usdt,
        **market_state.diagnostics,
    }
    if execution_tier.tier == "TIER_C":
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.FILTERED_OUT,
            direction=None,
            candidates=[],
            reason_text="execution_tier_c_rejected",
            filter_reasons=["execution_tier_c_rejected"],
            diagnostic=diagnostic,
        )
    if market_state.market_state == MARKET_STATE_UNSTABLE:
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.NO_SETUP,
            direction=None,
            candidates=[],
            reason_text="unstable_no_trade",
            filter_reasons=["unstable_no_trade"],
            diagnostic=diagnostic,
        )

    build_results: list[CandidateBuildResult] = []
    if market_state.market_state == MARKET_STATE_BULL:
        build_results.extend(
            (
                _coerce_build_result(
                    setup_family=SETUP_BREAKOUT,
                    build_output=_build_breakout_candidate(
                        symbol=symbol,
                        direction=SignalDirection.LONG,
                        candles_15m=candles_15m,
                        candles_1h=candles_1h,
                        current_price=current_price,
                        funding_rate=funding_rate,
                        next_funding_time_ms=next_funding_time_ms,
                        account_maker_fee_rate=account_maker_fee_rate,
                        account_taker_fee_rate=account_taker_fee_rate,
                        funding_rate_history=funding_rate_history,
                        execution_tier=execution_tier,
                        market_state=market_state,
                        config=config,
                        tick_size=tick_size,
                        with_diagnostics=True,
                    ),
                    entry_types_considered=["LIMIT_GTD", "STOP_ENTRY"],
                ),
                _coerce_build_result(
                    setup_family=SETUP_PULLBACK,
                    build_output=_build_pullback_candidate(
                        symbol=symbol,
                        direction=SignalDirection.LONG,
                        candles_15m=candles_15m,
                        candles_1h=candles_1h,
                        current_price=current_price,
                        funding_rate=funding_rate,
                        next_funding_time_ms=next_funding_time_ms,
                        account_maker_fee_rate=account_maker_fee_rate,
                        account_taker_fee_rate=account_taker_fee_rate,
                        funding_rate_history=funding_rate_history,
                        execution_tier=execution_tier,
                        market_state=market_state,
                        config=config,
                        tick_size=tick_size,
                        with_diagnostics=True,
                    ),
                    entry_types_considered=["LIMIT_GTD", "STOP_ENTRY"],
                ),
            )
        )
    elif market_state.market_state == MARKET_STATE_BEAR:
        build_results.extend(
            (
                _coerce_build_result(
                    setup_family=SETUP_BREAKOUT,
                    build_output=_build_breakout_candidate(
                        symbol=symbol,
                        direction=SignalDirection.SHORT,
                        candles_15m=candles_15m,
                        candles_1h=candles_1h,
                        current_price=current_price,
                        funding_rate=funding_rate,
                        next_funding_time_ms=next_funding_time_ms,
                        account_maker_fee_rate=account_maker_fee_rate,
                        account_taker_fee_rate=account_taker_fee_rate,
                        funding_rate_history=funding_rate_history,
                        execution_tier=execution_tier,
                        market_state=market_state,
                        config=config,
                        tick_size=tick_size,
                        with_diagnostics=True,
                    ),
                    entry_types_considered=["LIMIT_GTD", "STOP_ENTRY"],
                ),
                _coerce_build_result(
                    setup_family=SETUP_PULLBACK,
                    build_output=_build_pullback_candidate(
                        symbol=symbol,
                        direction=SignalDirection.SHORT,
                        candles_15m=candles_15m,
                        candles_1h=candles_1h,
                        current_price=current_price,
                        funding_rate=funding_rate,
                        next_funding_time_ms=next_funding_time_ms,
                        account_maker_fee_rate=account_maker_fee_rate,
                        account_taker_fee_rate=account_taker_fee_rate,
                        funding_rate_history=funding_rate_history,
                        execution_tier=execution_tier,
                        market_state=market_state,
                        config=config,
                        tick_size=tick_size,
                        with_diagnostics=True,
                    ),
                    entry_types_considered=["LIMIT_GTD", "STOP_ENTRY"],
                ),
            )
        )
    elif market_state.market_state == MARKET_STATE_RANGE:
        build_results.append(
            _coerce_build_result(
                setup_family=SETUP_RANGE,
                build_output=_build_range_candidate(
                    symbol=symbol,
                    candles_15m=candles_15m,
                    current_price=current_price,
                    funding_rate=funding_rate,
                    next_funding_time_ms=next_funding_time_ms,
                    account_maker_fee_rate=account_maker_fee_rate,
                    account_taker_fee_rate=account_taker_fee_rate,
                    funding_rate_history=funding_rate_history,
                    execution_tier=execution_tier,
                    market_state=market_state,
                    config=config,
                    tick_size=tick_size,
                    with_diagnostics=True,
                ),
                entry_types_considered=["LIMIT_GTD"],
            )
        )
    raw_candidates = [result.candidate for result in build_results if result.candidate is not None]
    setup_diagnostics = {
        result.setup_diagnostic.get("setup_family", f"setup_{index}"): result.setup_diagnostic
        for index, result in enumerate(build_results, start=1)
        if result.setup_diagnostic
    }
    if setup_diagnostics:
        diagnostic["aqrr_setup_diagnostics"] = setup_diagnostics

    if not raw_candidates:
        raw_rejection_reasons = _dedupe_reasons(
            [
                reason
                for result in build_results
                for reason in result.raw_rejection_reasons
            ]
        )
        reason_text = raw_rejection_reasons[0] if raw_rejection_reasons else "no_aqrr_setup"
        diagnostic.update(
            {
                "aqrr_raw_rejection_reason": raw_rejection_reasons[0] if raw_rejection_reasons else None,
                "aqrr_raw_rejection_reasons": raw_rejection_reasons,
                "aqrr_rejection_stage": "candidate_build",
            }
        )
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.NO_SETUP,
            direction=market_state.direction,
            candidates=[],
            reason_text=reason_text,
            filter_reasons=raw_rejection_reasons or ["no_aqrr_setup"],
            diagnostic=diagnostic,
        )

    candidate_pool: list[SetupCandidate] = []
    closes_1h_values = closes(candles_1h)
    symbol_returns_1h = percentage_returns(closes_1h_values[-73:])
    btc_corr = _correlation(symbol_returns_1h, btc_returns_1h or [])
    hard_filter_diagnostics: list[dict[str, object]] = []
    hard_filter_reasons: list[str] = []
    for candidate in raw_candidates:
        planned_notional, required_leverage = _required_leverage(
            entry_price=candidate.entry_price,
            stop_loss=candidate.stop_loss,
            available_balance=available_balance,
            filters_min_notional=filters_min_notional,
            config=config,
            remaining_entry_slots=remaining_entry_slots,
            remaining_portfolio_risk_usdt=remaining_portfolio_risk_usdt,
        )
        symbol_cluster = _cluster_for_symbol(symbol)
        candidate_hard_filter_diagnostic = {
            "setup_family": candidate.setup_family,
            "setup_variant": candidate.setup_variant,
            "entry_style": candidate.entry_style,
            "final_score": candidate.final_score,
            "score_breakdown": candidate.score_breakdown,
            "planned_notional": round(planned_notional, 8),
            "required_leverage": required_leverage,
        }
        raw_candidate_rejection_reasons: list[str] = []
        if required_leverage > config.max_leverage:
            raw_candidate_rejection_reasons.append("required_leverage_above_max")
        threshold_failures, threshold_diagnostic = _candidate_core_threshold_failures(candidate, config)
        raw_candidate_rejection_reasons.extend(threshold_failures)
        candidate_hard_filter_diagnostic.update(threshold_diagnostic)
        if raw_candidate_rejection_reasons:
            raw_candidate_rejection_reasons = _dedupe_reasons(raw_candidate_rejection_reasons)
            candidate_hard_filter_diagnostic["raw_rejection_reason"] = raw_candidate_rejection_reasons[0]
            candidate_hard_filter_diagnostic["raw_rejection_reasons"] = raw_candidate_rejection_reasons
            hard_filter_diagnostics.append(candidate_hard_filter_diagnostic)
            hard_filter_reasons.extend(raw_candidate_rejection_reasons)
            continue
        extra_context = {
            **candidate.extra_context,
            "market_state": candidate.market_state,
            "setup_family": candidate.setup_family,
            "setup_variant": candidate.setup_variant,
            "entry_style": candidate.entry_style,
            "execution_tier": candidate.execution_tier,
            "net_r_multiple": round(candidate.net_r_multiple, 4),
            "estimated_cost": round(candidate.estimated_cost, 8),
            "rank_value": round(candidate.rank_value, 4),
            "planned_notional": round(planned_notional, 8),
            "required_leverage": required_leverage,
            "btc_beta_correlation": round(btc_corr, 4),
            "cluster": symbol_cluster,
        }
        selection_context = {
            **candidate.selection_context,
            "returns_1h": symbol_returns_1h,
            "btc_beta_correlation": btc_corr,
            "cluster": symbol_cluster,
        }
        candidate_pool.append(
            SetupCandidate(
                **{
                    **candidate.__dict__,
                    "extra_context": extra_context,
                    "selection_context": selection_context,
                }
            )
        )

    if not candidate_pool:
        raw_rejection_reasons = _dedupe_reasons(hard_filter_reasons)
        reason_text = raw_rejection_reasons[0] if raw_rejection_reasons else "aqrr_hard_filters_failed"
        diagnostic.update(
            {
                "aqrr_raw_rejection_reason": raw_rejection_reasons[0] if raw_rejection_reasons else None,
                "aqrr_raw_rejection_reasons": raw_rejection_reasons,
                "aqrr_rejection_stage": "hard_filter",
                "aqrr_hard_filter_diagnostics": hard_filter_diagnostics,
            }
        )
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.FILTERED_OUT,
            direction=market_state.direction,
            candidates=[],
            reason_text=reason_text,
            filter_reasons=raw_rejection_reasons or ["aqrr_hard_filters_failed"],
            diagnostic=diagnostic,
        )

    diagnostic["aqrr_hard_filter_diagnostics"] = hard_filter_diagnostics
    return AqrrEvaluation(
        outcome=ScanSymbolOutcome.CANDIDATE,
        direction=candidate_pool[0].direction,
        candidates=rank_candidates(candidate_pool),
        reason_text="aqrr_candidate_ready",
        filter_reasons=[],
        diagnostic=diagnostic,
    )


def select_candidates(candidates: list[SetupCandidate], *, config: StrategyConfig) -> SelectionDecision:
    selected: list[SetupCandidate] = []
    rejected: dict[tuple[str, str, float], str] = {}
    cluster_counts: dict[str, int] = {}
    btc_beta_same_direction_count: dict[SignalDirection, int] = {}
    weak_quality_threshold = max(config.tier_b_min_score, 80)

    remaining = list(candidates)
    while remaining:
        candidate = rank_candidates(remaining, selected=selected)[0]
        remaining.remove(candidate)
        candidate_key = _candidate_key(candidate)
        if len(selected) >= config.max_entry_ideas:
            rejected[candidate_key] = "slot_limit_reached"
            continue

        selection_context = candidate.selection_context
        candidate_cluster_raw = selection_context.get("cluster")
        candidate_cluster = str(candidate_cluster_raw) if candidate_cluster_raw else None
        if candidate_cluster is not None:
            cluster_count = cluster_counts.get(candidate_cluster, 0)
            if cluster_count >= 2:
                rejected[candidate_key] = "cluster_conflict"
                continue
            if cluster_count >= 1 and candidate.final_score < weak_quality_threshold:
                rejected[candidate_key] = "cluster_conflict"
                continue

        candidate_returns = selection_context.get("returns_1h") or []
        violates_correlation = False
        for existing in selected:
            existing_returns = existing.selection_context.get("returns_1h") or []
            correlation = _correlation(candidate_returns, existing_returns)
            same_effective_direction = (
                existing.direction == candidate.direction and correlation >= 0
            )
            if not same_effective_direction:
                continue
            if abs(correlation) > float(config.correlation_reject_threshold):
                violates_correlation = True
                break
        if violates_correlation:
            rejected[candidate_key] = "correlation_conflict"
            continue

        candidate_beta = float(selection_context.get("btc_beta_correlation") or 0.0)
        if (
            candidate_beta > 0.70
            and btc_beta_same_direction_count.get(candidate.direction, 0) >= 2
        ):
            rejected[candidate_key] = "btc_beta_conflict"
            continue

        selected.append(candidate)
        if candidate_cluster is not None:
            cluster_counts[candidate_cluster] = cluster_counts.get(candidate_cluster, 0) + 1
        if candidate_beta > 0.70:
            btc_beta_same_direction_count[candidate.direction] = (
                btc_beta_same_direction_count.get(candidate.direction, 0) + 1
            )

    return SelectionDecision(selected=selected, rejected=rejected)
