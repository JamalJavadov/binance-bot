from datetime import datetime

from pydantic import BaseModel


class HealthStatusResponse(BaseModel):
    backend_ok: bool
    db_ok: bool
    binance_reachable: bool
    server_time_offset_ms: int | None = None


class BalanceResponse(BaseModel):
    asset: str
    balance: float
    available_balance: float
    usable_balance: float
    reserve_balance: float


class PositionResponse(BaseModel):
    symbol: str
    position_side: str
    direction: str
    position_amount: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int | None = None
    source_kind: str
    linked_order_id: int | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    closed_at: datetime | None = None


class PortfolioSummaryResponse(BaseModel):
    open_position_count: int
    winning_position_count: int
    losing_position_count: int
    total_unrealized_pnl: float
    last_synced_at: datetime | None = None
