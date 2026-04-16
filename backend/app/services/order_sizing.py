from decimal import Decimal


def resolve_risk_per_trade_fraction(
    settings_map: dict[str, str],
    *,
    fallback_pct: Decimal = Decimal("1.0"),
    setting_key: str = "risk_per_trade_pct",
) -> Decimal:
    raw_value = settings_map.get(setting_key)
    if raw_value is None:
        return fallback_pct / Decimal("100")
    try:
        value = Decimal(str(raw_value))
    except Exception:
        return fallback_pct / Decimal("100")
    if value <= 0:
        return fallback_pct / Decimal("100")
    return value / Decimal("100")


def calculate_stop_distance_pct(*, entry_price: Decimal, stop_loss_price: Decimal) -> Decimal:
    if entry_price <= 0:
        return Decimal("0")
    return abs(entry_price - stop_loss_price) / entry_price


def calculate_position_size_usdt(
    *,
    risk_budget_usdt: Decimal,
    stop_distance_pct: Decimal,
) -> Decimal:
    if risk_budget_usdt <= 0 or stop_distance_pct <= 0:
        return Decimal("0")
    return risk_budget_usdt / stop_distance_pct
