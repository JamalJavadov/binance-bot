from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class AutoModeUpdateRequest(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None


class AutoModeStatusRead(BaseModel):
    enabled: bool
    paused: bool
    running: bool
    signal_schedule: str
    kill_switch_active: bool
    kill_switch_reason: str | None = None
    active_order_count: int
    active_risk_usdt: Decimal
    portfolio_risk_budget_usdt: Decimal
    per_slot_risk_budget_usdt: Decimal
    last_cycle_started_at: datetime | None = None
    last_cycle_completed_at: datetime | None = None
    next_cycle_at: datetime | None = None
