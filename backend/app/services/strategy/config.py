from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class StrategyConfig:
    risk_per_trade_pct: Decimal
    max_portfolio_risk_pct: Decimal
    max_leverage: int
    deployable_equity_pct: Decimal
    max_book_spread_bps: Decimal
    min_24h_quote_volume_usdt: Decimal
    kill_switch_consecutive_stop_losses: int
    kill_switch_daily_drawdown_pct: Decimal
    auto_mode_max_entry_drift_pct: Decimal
    breakout_lookback_bars: int = 20
    range_lookback_bars: int = 24
    trend_adx_threshold: int = 22
    range_adx_threshold: int = 18
    atr_period_15m: int = 14
    atr_period_1h: int = 14
    atr_period_4h: int = 14
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    ema_context_period: int = 200
    bollinger_period: int = 20
    bollinger_std_mult: Decimal = Decimal("2.0")
    correlation_reject_threshold: Decimal = Decimal("0.80")
    tier_a_min_score: int = 70
    tier_b_min_score: int = 78
    breakout_retest_expiry_bars: int = 3
    pullback_expiry_bars: int = 4
    range_expiry_bars: int = 3
    max_entry_ideas: int = 3
    max_pending_entry_orders: int = 3
    max_open_positions: int = 3
    stop_buffer_atr_fraction: Decimal = Decimal("0.20")
    breakout_entry_zone_atr_fraction: Decimal = Decimal("0.10")
    breakout_volume_ratio_min: Decimal = Decimal("1.20")
    breakout_body_fraction_min: Decimal = Decimal("0.55")
    breakout_range_atr_cap: Decimal = Decimal("1.80")
    pullback_confirmation_body_fraction_min: Decimal = Decimal("0.45")
    range_touch_fraction: Decimal = Decimal("0.12")
    min_net_r_multiple: Decimal = Decimal("3.0")
    volatility_shock_range_multiple: Decimal = Decimal("2.5")
    spread_tier_a_bps: Decimal = Decimal("6")
    spread_tier_b_bps: Decimal = Decimal("12")
    maker_fee_rate: Decimal = Decimal("0.0004")
    taker_fee_rate: Decimal = Decimal("0.0006")
    slippage_rate_floor: Decimal = Decimal("0.0004")

    @property
    def risk_per_trade_fraction(self) -> Decimal:
        return self.risk_per_trade_pct / Decimal("100")

    @property
    def max_portfolio_risk_fraction(self) -> Decimal:
        return self.max_portfolio_risk_pct / Decimal("100")

    @property
    def deployable_equity_fraction(self) -> Decimal:
        return self.deployable_equity_pct / Decimal("100")

    @property
    def max_book_spread_fraction(self) -> Decimal:
        return self.max_book_spread_bps / Decimal("10000")

    @property
    def kill_switch_daily_drawdown_fraction(self) -> Decimal:
        return self.kill_switch_daily_drawdown_pct / Decimal("100")

    @property
    def auto_mode_max_entry_distance_fraction(self) -> Decimal:
        return self.auto_mode_max_entry_drift_pct / Decimal("100")


def _parse_decimal(value: object, *, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return default


def _resolve_decimal(
    settings_map: dict[str, str],
    *,
    key: str,
    default: Decimal,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    value = _parse_decimal(settings_map.get(key, default), default=default)
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def _resolve_int(
    settings_map: dict[str, str],
    *,
    key: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        value = int(str(settings_map.get(key, default)).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def resolve_strategy_config(settings_map: dict[str, str]) -> StrategyConfig:
    return StrategyConfig(
        risk_per_trade_pct=_resolve_decimal(
            settings_map,
            key="risk_per_trade_pct",
            default=Decimal("2.0"),
            minimum=Decimal("0.1"),
            maximum=Decimal("2.0"),
        ),
        max_portfolio_risk_pct=_resolve_decimal(
            settings_map,
            key="max_portfolio_risk_pct",
            default=Decimal("6.0"),
            minimum=Decimal("0.5"),
            maximum=Decimal("10.0"),
        ),
        max_leverage=_resolve_int(settings_map, key="max_leverage", default=10, minimum=1, maximum=125),
        deployable_equity_pct=_resolve_decimal(
            settings_map,
            key="deployable_equity_pct",
            default=Decimal("90"),
            minimum=Decimal("50"),
            maximum=Decimal("100"),
        ),
        max_book_spread_bps=_resolve_decimal(
            settings_map,
            key="max_book_spread_bps",
            default=Decimal("12"),
            minimum=Decimal("1"),
            maximum=Decimal("100"),
        ),
        min_24h_quote_volume_usdt=_resolve_decimal(
            settings_map,
            key="min_24h_quote_volume_usdt",
            default=Decimal("25000000"),
            minimum=Decimal("0"),
        ),
        kill_switch_consecutive_stop_losses=_resolve_int(
            settings_map,
            key="kill_switch_consecutive_stop_losses",
            default=2,
            minimum=1,
            maximum=10,
        ),
        kill_switch_daily_drawdown_pct=_resolve_decimal(
            settings_map,
            key="kill_switch_daily_drawdown_pct",
            default=Decimal("4.0"),
            minimum=Decimal("1.0"),
            maximum=Decimal("20.0"),
        ),
        auto_mode_max_entry_drift_pct=_resolve_decimal(
            settings_map,
            key="auto_mode_max_entry_drift_pct",
            default=Decimal("5.0"),
            minimum=Decimal("0.5"),
            maximum=Decimal("10.0"),
        ),
        maker_fee_rate=_resolve_decimal(
            settings_map,
            key="maker_fee_rate",
            default=Decimal("0.0004"),
            minimum=Decimal("0"),
            maximum=Decimal("0.01"),
        ),
        taker_fee_rate=_resolve_decimal(
            settings_map,
            key="taker_fee_rate",
            default=Decimal("0.0006"),
            minimum=Decimal("0"),
            maximum=Decimal("0.02"),
        ),
    )
