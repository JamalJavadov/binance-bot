from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import OrderStatus, SignalDirection
from app.schemas.common import ORMModel


class OrderRead(ORMModel):
    id: int
    signal_id: int | None = None
    symbol: str
    direction: SignalDirection
    leverage: int
    margin_type: str
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    rank_value: Decimal | None = None
    net_r_multiple: Decimal | None = None
    estimated_cost: Decimal | None = None
    entry_style: str | None = None
    setup_family: str | None = None
    setup_variant: str | None = None
    market_state: str | None = None
    execution_tier: str | None = None
    score_band: str | None = None
    volatility_band: str | None = None
    stats_bucket_key: str | None = None
    strategy_context: dict = Field(default_factory=dict)
    quantity: Decimal
    position_margin: Decimal
    notional_value: Decimal
    rr_ratio: Decimal
    entry_order_id: str | None = None
    tp_order_id: str | None = None
    partial_tp_enabled: bool | None = None
    take_profit_1: Decimal | None = None
    take_profit_2: Decimal | None = None
    tp_quantity_1: Decimal | None = None
    tp_quantity_2: Decimal | None = None
    tp_order_1_id: str | None = None
    tp_order_2_id: str | None = None
    tp1_filled_at: datetime | None = None
    remaining_quantity: Decimal | None = None
    sl_order_id: str | None = None
    status: OrderStatus
    placed_at: datetime | None = None
    triggered_at: datetime | None = None
    closed_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    expires_at: datetime
    realized_pnl: Decimal | None = None
    close_price: Decimal | None = None
    close_type: str | None = None
    risk_budget_usdt: Decimal
    risk_usdt_at_stop: Decimal
    risk_pct_of_wallet: Decimal
    approved_by: str
    created_at: datetime
    updated_at: datetime


class OrderPreviewRead(BaseModel):
    status: str
    can_place: bool
    auto_resized: bool
    requested_quantity: Decimal
    final_quantity: Decimal
    max_affordable_quantity: Decimal
    mark_price_used: Decimal
    entry_notional: Decimal
    required_initial_margin: Decimal
    estimated_entry_fee: Decimal
    available_balance: Decimal
    reserve_balance: Decimal
    usable_balance: Decimal
    risk_budget_usdt: Decimal
    risk_usdt_at_stop: Decimal
    recommended_leverage: int
    reason: str | None = None
