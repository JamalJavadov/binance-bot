from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.services.audit import record_audit
from app.models.settings import SettingsEntry


AUTO_MODE_MAX_ENTRY_DRIFT_PCT_DEFAULT = Decimal("5.0")
AUTO_MODE_MAX_ENTRY_DRIFT_PCT_MIN = Decimal("0.5")
AUTO_MODE_MAX_ENTRY_DRIFT_PCT_MAX = Decimal("10.0")
AUTO_MODE_MAX_ENTRY_DRIFT_PCT_ERROR = "Auto entry drift percent must be between 0.5 and 10.0"
PUBLIC_SETTING_KEYS = {
    "auto_mode_enabled",
    "auto_mode_paused",
    "risk_per_trade_pct",
    "max_portfolio_risk_pct",
    "max_leverage",
    "deployable_equity_pct",
    "max_book_spread_bps",
    "min_24h_quote_volume_usdt",
    "kill_switch_consecutive_stop_losses",
    "kill_switch_daily_drawdown_pct",
    "auto_mode_max_entry_drift_pct",
}

DEFAULT_SETTINGS: dict[str, str] = {
    "auto_mode_enabled": "false",
    "auto_mode_paused": "false",
    "risk_per_trade_pct": "2.0",
    "max_portfolio_risk_pct": "6.0",
    "max_leverage": "10",
    "deployable_equity_pct": "90",
    "max_book_spread_bps": "12",
    "min_24h_quote_volume_usdt": "25000000",
    "kill_switch_consecutive_stop_losses": "2",
    "kill_switch_daily_drawdown_pct": "4.0",
    "auto_mode_max_entry_drift_pct": "5.0",
}

def _filter_public_settings(mapping: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in mapping.items()
        if key in PUBLIC_SETTING_KEYS
    }


def _migrate_legacy_risk_fraction(raw_value: str | None) -> tuple[str, str]:
    if raw_value is None:
        return DEFAULT_SETTINGS["risk_per_trade_pct"], "defaulted_missing_legacy_value"
    try:
        parsed = Decimal(str(raw_value).strip())
    except (InvalidOperation, ValueError):
        return DEFAULT_SETTINGS["risk_per_trade_pct"], "defaulted_invalid_legacy_value"
    if parsed > 0 and parsed <= Decimal("0.05"):
        return str((parsed * Decimal("100")).normalize()), "converted_fraction_to_percent"
    return DEFAULT_SETTINGS["risk_per_trade_pct"], "defaulted_unsafe_legacy_value"


async def _ensure_default_settings(session) -> dict[str, SettingsEntry]:
    existing = {
        row.key: row
        for row in (await session.execute(select(SettingsEntry))).scalars().all()
    }
    changed = False
    legacy_risk_fraction = existing.get("risk_fraction")
    risk_per_trade_entry = existing.get("risk_per_trade_pct")
    if risk_per_trade_entry is None:
        migrated_value, reason = _migrate_legacy_risk_fraction(
            legacy_risk_fraction.value if legacy_risk_fraction is not None else None
        )
        risk_per_trade_entry = SettingsEntry(key="risk_per_trade_pct", value=migrated_value)
        session.add(risk_per_trade_entry)
        existing["risk_per_trade_pct"] = risk_per_trade_entry
        changed = True
        await record_audit(
            session,
            event_type="SETTINGS_MIGRATED",
            message="Strategy settings migrated to AQRR controls",
            details={
                "migrated_key": "risk_per_trade_pct",
                "legacy_key": "risk_fraction",
                "reason": reason,
                "value": migrated_value,
            },
        )
    for key, value in DEFAULT_SETTINGS.items():
        if key in existing:
            continue
        session.add(SettingsEntry(key=key, value=value))
        changed = True
    if changed:
        await session.commit()
        existing = {
            row.key: row
            for row in (await session.execute(select(SettingsEntry))).scalars().all()
        }
    return existing


async def seed_settings(session) -> None:
    await _ensure_default_settings(session)


async def get_settings_map(session) -> dict[str, str]:
    rows = (await _ensure_default_settings(session)).values()
    mapping = {row.key: row.value for row in rows}
    for key, value in DEFAULT_SETTINGS.items():
        mapping.setdefault(key, value)
    return _filter_public_settings(mapping)


def _normalize_bool_setting(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{label} must be true or false")
    return normalized


def _parse_auto_mode_max_entry_drift_pct(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(AUTO_MODE_MAX_ENTRY_DRIFT_PCT_ERROR) from exc
    if parsed < AUTO_MODE_MAX_ENTRY_DRIFT_PCT_MIN or parsed > AUTO_MODE_MAX_ENTRY_DRIFT_PCT_MAX:
        raise ValueError(AUTO_MODE_MAX_ENTRY_DRIFT_PCT_ERROR)
    return parsed


def _parse_decimal_range(
    value: object,
    *,
    label: str,
    minimum: Decimal,
    maximum: Decimal,
) -> str:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be between {minimum} and {maximum}") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return str(parsed.normalize())


def _parse_positive_decimal(value: object, *, label: str) -> str:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive number")
    return str(parsed.normalize())


def _parse_int_range(value: object, *, label: str, minimum: int, maximum: int) -> str:
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be between {minimum} and {maximum}") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return str(parsed)


def resolve_auto_mode_max_entry_drift_pct(settings_map: dict[str, str]) -> Decimal:
    raw_value = settings_map.get("auto_mode_max_entry_drift_pct", DEFAULT_SETTINGS["auto_mode_max_entry_drift_pct"])
    try:
        return _parse_auto_mode_max_entry_drift_pct(raw_value)
    except ValueError:
        return AUTO_MODE_MAX_ENTRY_DRIFT_PCT_DEFAULT


def resolve_auto_mode_max_entry_distance_fraction(settings_map: dict[str, str]) -> Decimal:
    return resolve_auto_mode_max_entry_drift_pct(settings_map) / Decimal("100")


def normalize_setting_value(key: str, value: object) -> str:
    text = str(value).strip()
    if key not in PUBLIC_SETTING_KEYS:
        raise ValueError(f"{key} is not a supported AQRR setting")
    if key in {"auto_mode_enabled", "auto_mode_paused"}:
        return _normalize_bool_setting(text, label=key.replace("_", " "))
    if key == "risk_per_trade_pct":
        return _parse_decimal_range(
            text,
            label="risk per trade percent",
            minimum=Decimal("0.1"),
            maximum=Decimal("2.0"),
        )
    if key == "max_portfolio_risk_pct":
        return _parse_decimal_range(
            text,
            label="max portfolio risk percent",
            minimum=Decimal("0.5"),
            maximum=Decimal("10.0"),
        )
    if key == "max_leverage":
        return _parse_int_range(text, label="max leverage", minimum=1, maximum=10)
    if key == "deployable_equity_pct":
        return _parse_decimal_range(
            text,
            label="deployable equity percent",
            minimum=Decimal("50"),
            maximum=Decimal("100"),
        )
    if key == "auto_mode_max_entry_drift_pct":
        return str(_parse_auto_mode_max_entry_drift_pct(text).normalize())
    if key == "min_24h_quote_volume_usdt":
        return _parse_positive_decimal(text, label="minimum 24h quote volume")
    if key == "max_book_spread_bps":
        return _parse_decimal_range(
            text,
            label="maximum book spread bps",
            minimum=Decimal("1"),
            maximum=Decimal("100"),
        )
    if key == "kill_switch_consecutive_stop_losses":
        return _parse_int_range(
            text,
            label="kill switch consecutive stop losses",
            minimum=1,
            maximum=10,
        )
    if key == "kill_switch_daily_drawdown_pct":
        return _parse_decimal_range(
            text,
            label="kill switch daily drawdown percent",
            minimum=Decimal("1.0"),
            maximum=Decimal("20.0"),
        )
    return text


async def patch_settings(session, updates: dict[str, str]) -> dict[str, str]:
    rows = await _ensure_default_settings(session)
    alias_updates = dict(updates)
    if "risk_fraction" in alias_updates and "risk_per_trade_pct" not in alias_updates:
        migrated_value, _reason = _migrate_legacy_risk_fraction(str(alias_updates["risk_fraction"]))
        alias_updates["risk_per_trade_pct"] = migrated_value
    alias_updates.pop("risk_fraction", None)
    for key, value in alias_updates.items():
        normalized_value = normalize_setting_value(key, value)
        if key in rows:
            rows[key].value = normalized_value
        else:
            session.add(SettingsEntry(key=key, value=normalized_value))
    await session.commit()
    return await get_settings_map(session)
