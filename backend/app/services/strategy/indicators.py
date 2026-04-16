from __future__ import annotations

from math import sqrt
from statistics import median

from app.services.strategy.adx import calculate_atr
from app.services.strategy.types import Candle

VOLATILITY_SHOCK_LOOKBACK_DAYS = 30
VOLATILITY_SHOCK_15M_BARS_PER_DAY = 96
VOLATILITY_SHOCK_15M_LOOKBACK_BARS = VOLATILITY_SHOCK_LOOKBACK_DAYS * VOLATILITY_SHOCK_15M_BARS_PER_DAY


def closes(candles: list[Candle]) -> list[float]:
    return [candle.close for candle in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [candle.high for candle in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [candle.low for candle in candles]


def volumes(candles: list[Candle]) -> list[float]:
    return [candle.volume for candle in candles]


def ema_series(values: list[float], period: int) -> list[float]:
    if period <= 0 or not values:
        return []
    multiplier = 2 / (period + 1)
    ema_values: list[float] = [values[0]]
    current = values[0]
    for value in values[1:]:
        current = ((value - current) * multiplier) + current
        ema_values.append(current)
    return ema_values


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return None if not series else series[-1]


def sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / len(window)


def rolling_std(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    avg = sum(window) / len(window)
    variance = sum((value - avg) ** 2 for value in window) / len(window)
    return sqrt(variance)


def bollinger_bandwidth(values: list[float], period: int, std_mult: float) -> float | None:
    middle = sma(values, period)
    std_value = rolling_std(values, period)
    if middle is None or std_value is None or middle == 0:
        return None
    upper = middle + (std_value * std_mult)
    lower = middle - (std_value * std_mult)
    return (upper - lower) / middle


def historical_bollinger_bandwidths(values: list[float], period: int, std_mult: float) -> list[float]:
    bandwidths: list[float] = []
    if period <= 0 or len(values) < period:
        return bandwidths
    for index in range(period, len(values) + 1):
        window = values[:index]
        bandwidth = bollinger_bandwidth(window, period, std_mult)
        if bandwidth is not None:
            bandwidths.append(bandwidth)
    return bandwidths


def rsi(values: list[float], period: int = 14) -> float | None:
    if period <= 0 or len(values) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values[:-1], values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def body_fraction(candle: Candle) -> float:
    candle_range = candle.range_size
    if candle_range <= 0:
        return 0.0
    return abs(candle.close - candle.open) / candle_range


def lower_shadow_fraction(candle: Candle) -> float:
    candle_range = candle.range_size
    if candle_range <= 0:
        return 0.0
    shadow = min(candle.open, candle.close) - candle.low
    return shadow / candle_range


def upper_shadow_fraction(candle: Candle) -> float:
    candle_range = candle.range_size
    if candle_range <= 0:
        return 0.0
    shadow = candle.high - max(candle.open, candle.close)
    return shadow / candle_range


def volume_ratio(candles: list[Candle], lookback: int = 20) -> float:
    if len(candles) < max(lookback, 2):
        return 0.0
    current = candles[-1].volume
    baseline = sum(candle.volume for candle in candles[-lookback - 1 : -1]) / lookback
    if baseline <= 0:
        return 0.0
    return current / baseline


def mean_cross_count(closes_values: list[float], ema_values: list[float], lookback: int) -> int:
    if lookback <= 1 or len(closes_values) < lookback or len(ema_values) < lookback:
        return 0
    signs: list[int] = []
    for close_value, ema_value in zip(closes_values[-lookback:], ema_values[-lookback:]):
        if close_value > ema_value:
            signs.append(1)
        elif close_value < ema_value:
            signs.append(-1)
        else:
            signs.append(0)
    count = 0
    previous = signs[0]
    for current in signs[1:]:
        if current == 0:
            continue
        if previous != 0 and current != previous:
            count += 1
        previous = current
    return count


def normalized_ema_slope(ema_values: list[float], atr_value: float | None, lookback: int = 10) -> float:
    if atr_value is None or atr_value <= 0 or len(ema_values) <= lookback:
        return 0.0
    delta = ema_values[-1] - ema_values[-1 - lookback]
    return delta / (lookback * atr_value)


def percentage_returns(values: list[float]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(values[:-1], values[1:]):
        if previous == 0:
            returns.append(0.0)
        else:
            returns.append((current - previous) / previous)
    return returns


def percentile_rank(values: list[float], current: float) -> float:
    if not values:
        return 0.0
    less_or_equal = sum(1 for value in values if value <= current)
    return less_or_equal / len(values)


def recent_median_range(candles: list[Candle], lookback: int = 50) -> float:
    if not candles:
        return 0.0
    sample = candles[-lookback:]
    return median([candle.range_size for candle in sample]) if sample else 0.0


def required_15m_candles_for_volatility_shock(*, atr_period: int = 14) -> int:
    # +1 allows the caller to include the still-forming candle and rely on closed_candles().
    return VOLATILITY_SHOCK_15M_LOOKBACK_BARS + max(atr_period, 1) + 1


def volatility_shock_flag(
    candles_15m: list[Candle],
    *,
    atr_period: int = 14,
    range_multiple: float = 2.5,
) -> tuple[bool, dict[str, float | None]]:
    atr_history_pct: list[float] = []
    closes_15m = closes(candles_15m)
    for index in range(atr_period + 1, len(candles_15m) + 1):
        atr_value = calculate_atr(candles_15m[:index], period=atr_period)
        close_value = closes_15m[index - 1]
        if atr_value is not None and close_value > 0:
            atr_history_pct.append(atr_value / close_value)
    atr_window = atr_history_pct[-VOLATILITY_SHOCK_15M_LOOKBACK_BARS:]
    current_atr_pct = atr_window[-1] if atr_window else None
    atr_percentile = percentile_rank(atr_window, current_atr_pct) if current_atr_pct is not None else 0.0
    median_range = recent_median_range(candles_15m, lookback=50)
    current_range = candles_15m[-1].range_size if candles_15m else 0.0
    shock = bool(
        (current_atr_pct is not None and atr_percentile > 0.97)
        or (median_range > 0 and current_range > (median_range * range_multiple))
    )
    return shock, {
        "atr_pct": current_atr_pct,
        "atr_percentile": atr_percentile,
        "atr_percentile_window_bars": float(len(atr_window)),
        "atr_percentile_window_target_bars": float(VOLATILITY_SHOCK_15M_LOOKBACK_BARS),
        "median_range_50": median_range,
        "current_range": current_range,
    }
