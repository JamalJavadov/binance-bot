from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import SignalDirection, SignalStatus, TriggerType
from app.schemas.common import ORMModel
from app.schemas.order import OrderPreviewRead


class SignalRead(ORMModel):
    id: int
    scan_cycle_id: int | None = None
    scan_trigger_type: TriggerType | None = None
    strategy_key: str | None = None
    strategy_label: str | None = None
    symbol: str
    direction: SignalDirection
    timeframe: str
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    rr_ratio: Decimal
    confirmation_score: int
    final_score: int
    rank_value: Decimal | None = None
    net_r_multiple: Decimal | None = None
    estimated_cost: Decimal | None = None
    score_breakdown: dict
    reason_text: str | None = None
    swing_origin: Decimal | None = None
    swing_terminus: Decimal | None = None
    fib_0786_level: Decimal | None = None
    current_price_at_signal: Decimal | None = None
    entry_style: str | None = None
    setup_family: str | None = None
    setup_variant: str | None = None
    market_state: str | None = None
    execution_tier: str | None = None
    score_band: str | None = None
    volatility_band: str | None = None
    stats_bucket_key: str | None = None
    strategy_context: dict = Field(default_factory=dict)
    expires_at: datetime
    status: SignalStatus
    extra_context: dict
    created_at: datetime
    updated_at: datetime


class SignalLiveReadinessRead(BaseModel):
    mark_price: Decimal | None = None
    order_preview: OrderPreviewRead | None = None
    can_open_now: bool
    failure_reason: str | None = None


class SignalRecommendationRead(BaseModel):
    rank: int
    signal: SignalRead
    live_readiness: SignalLiveReadinessRead


class SignalRecommendationsRead(BaseModel):
    latest_completed_scan_id: int | None = None
    latest_completed_scan_trigger_type: TriggerType | None = None
    latest_completed_scan_strategy_key: str | None = None
    latest_completed_scan_strategy_label: str | None = None
    refreshed_at: datetime
    items: list[SignalRecommendationRead]
