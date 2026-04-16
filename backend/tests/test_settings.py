from decimal import Decimal

import pytest

from app.models.settings import SettingsEntry
from app.services.settings import (
    DEFAULT_SETTINGS,
    get_settings_map,
    normalize_setting_value,
    patch_settings,
    resolve_auto_mode_max_entry_distance_fraction,
    resolve_auto_mode_max_entry_drift_pct,
)


def test_auto_mode_max_entry_drift_default_is_present() -> None:
    assert DEFAULT_SETTINGS["auto_mode_max_entry_drift_pct"] == "5.0"
    assert DEFAULT_SETTINGS["risk_per_trade_pct"] == "2.0"
    assert DEFAULT_SETTINGS["max_portfolio_risk_pct"] == "6.0"
    assert DEFAULT_SETTINGS["kill_switch_daily_drawdown_pct"] == "4.0"


def test_normalize_setting_value_accepts_auto_mode_max_entry_drift_pct() -> None:
    assert normalize_setting_value("auto_mode_max_entry_drift_pct", "5.0") == "5"


@pytest.mark.parametrize("value", ["0.4", "10.1", "not-a-number"])
def test_normalize_setting_value_rejects_invalid_auto_mode_max_entry_drift_pct(value: str) -> None:
    with pytest.raises(ValueError, match="between 0.5 and 10.0"):
        normalize_setting_value("auto_mode_max_entry_drift_pct", value)

def test_resolve_auto_mode_max_entry_drift_pct_uses_default_for_invalid_saved_value() -> None:
    assert resolve_auto_mode_max_entry_drift_pct({"auto_mode_max_entry_drift_pct": "invalid"}) == Decimal("5.0")
    assert resolve_auto_mode_max_entry_distance_fraction({"auto_mode_max_entry_drift_pct": "invalid"}) == Decimal("0.05")


def test_normalize_setting_value_accepts_risk_per_trade_pct() -> None:
    assert normalize_setting_value("risk_per_trade_pct", "1.5") == "1.5"


@pytest.mark.parametrize("value", ["0.05", "2.5", "not-a-number"])
def test_normalize_setting_value_rejects_invalid_risk_per_trade_pct(value: str) -> None:
    with pytest.raises(ValueError, match="risk per trade percent must be between 0.1 and 2.0"):
        normalize_setting_value("risk_per_trade_pct", value)


class FakeScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeExecuteResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return FakeScalarResult(self.rows)


class FakeSettingsSession:
    def __init__(self) -> None:
        self.rows: dict[str, SettingsEntry] = {}
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)
        if isinstance(obj, SettingsEntry):
            self.rows[obj.key] = obj

    async def execute(self, _statement):
        return FakeExecuteResult(list(self.rows.values()))

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_settings_service_ignores_legacy_scan_top_symbols_row() -> None:
    session = FakeSettingsSession()
    session.rows["scan_top_symbols"] = SettingsEntry(key="scan_top_symbols", value="250")

    initial_settings = await get_settings_map(session)
    assert "scan_top_symbols" not in initial_settings

    updated_settings = await patch_settings(session, {"max_leverage": "8"})
    assert "scan_top_symbols" not in updated_settings

    round_tripped_settings = await get_settings_map(session)
    assert "scan_top_symbols" not in round_tripped_settings
