from dataclasses import dataclass

from app.models.enums import SignalDirection
from app.services.strategy.types import Candle


@dataclass(frozen=True)
class TrendMetrics:
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    di_spread: float | None

    def aligned_di_spread(self, direction: SignalDirection) -> float:
        if self.di_spread is None:
            return 0.0
        return self.di_spread if direction == SignalDirection.LONG else -self.di_spread


def calculate_atr(candles: list[Candle], period: int = 14) -> float | None:
    if period <= 0 or len(candles) <= period:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = ((atr * (period - 1)) + tr) / period
    return atr


def calculate_trend_metrics(candles: list[Candle], period: int = 14) -> TrendMetrics:
    if period <= 0 or len(candles) <= period * 2:
        return TrendMetrics(adx=None, plus_di=None, minus_di=None, di_spread=None)

    true_ranges: list[float] = []
    plus_dm_values: list[float] = []
    minus_dm_values: list[float] = []

    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        upward_move = current.high - previous.high
        downward_move = previous.low - current.low
        plus_dm_values.append(upward_move if upward_move > downward_move and upward_move > 0 else 0.0)
        minus_dm_values.append(downward_move if downward_move > upward_move and downward_move > 0 else 0.0)
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )

    smoothed_tr = sum(true_ranges[:period])
    smoothed_plus_dm = sum(plus_dm_values[:period])
    smoothed_minus_dm = sum(minus_dm_values[:period])
    dx_values: list[float] = []
    latest_plus_di: float | None = None
    latest_minus_di: float | None = None

    for index in range(period, len(true_ranges)):
        if index > period:
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + true_ranges[index]
            smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_values[index]
            smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_values[index]

        if smoothed_tr <= 0:
            dx_values.append(0.0)
            latest_plus_di = 0.0
            latest_minus_di = 0.0
            continue

        latest_plus_di = 100.0 * smoothed_plus_dm / smoothed_tr
        latest_minus_di = 100.0 * smoothed_minus_dm / smoothed_tr
        denominator = latest_plus_di + latest_minus_di
        dx_values.append(0.0 if denominator <= 0 else 100.0 * abs(latest_plus_di - latest_minus_di) / denominator)

    if len(dx_values) < period:
        return TrendMetrics(adx=None, plus_di=latest_plus_di, minus_di=latest_minus_di, di_spread=None if latest_plus_di is None or latest_minus_di is None else latest_plus_di - latest_minus_di)

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period
    di_spread = None if latest_plus_di is None or latest_minus_di is None else latest_plus_di - latest_minus_di
    return TrendMetrics(
        adx=adx,
        plus_di=latest_plus_di,
        minus_di=latest_minus_di,
        di_spread=di_spread,
    )


def calculate_adx(candles: list[Candle], period: int = 14, *, include_components: bool = False) -> float | TrendMetrics | None:
    metrics = calculate_trend_metrics(candles, period=period)
    if include_components:
        return metrics
    return metrics.adx
