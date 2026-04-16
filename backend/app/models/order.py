from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import OrderStatus, SignalDirection


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"))
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[SignalDirection] = mapped_column(Enum(SignalDirection), nullable=False)
    leverage: Mapped[int] = mapped_column(nullable=False)
    margin_type: Mapped[str] = mapped_column(String(20), default="ISOLATED", nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    take_profit: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    rank_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    net_r_multiple: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    estimated_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    entry_style: Mapped[Optional[str]] = mapped_column(String(20))
    setup_family: Mapped[Optional[str]] = mapped_column(String(50))
    setup_variant: Mapped[Optional[str]] = mapped_column(String(100))
    market_state: Mapped[Optional[str]] = mapped_column(String(50))
    execution_tier: Mapped[Optional[str]] = mapped_column(String(20))
    score_band: Mapped[Optional[str]] = mapped_column(String(20))
    volatility_band: Mapped[Optional[str]] = mapped_column(String(20))
    stats_bucket_key: Mapped[Optional[str]] = mapped_column(String(255))
    strategy_context: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    position_margin: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    notional_value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    rr_ratio: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    entry_order_id: Mapped[Optional[str]] = mapped_column(String(100))
    tp_order_id: Mapped[Optional[str]] = mapped_column(String(100))
    partial_tp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    take_profit_1: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    take_profit_2: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    tp_quantity_1: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    tp_quantity_2: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    tp_order_1_id: Mapped[Optional[str]] = mapped_column(String(100))
    tp_order_2_id: Mapped[Optional[str]] = mapped_column(String(100))
    tp1_filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    remaining_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    sl_order_id: Mapped[Optional[str]] = mapped_column(String(100))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING_APPROVAL, nullable=False)
    placed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    close_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    close_type: Mapped[Optional[str]] = mapped_column(String(20))
    risk_budget_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    risk_usdt_at_stop: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("0"))
    risk_pct_of_wallet: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    approved_by: Mapped[str] = mapped_column(String(20), default="AUTO_MODE", nullable=False)

    signal = relationship("Signal", back_populates="orders")
    observed_positions = relationship("ObservedPosition", back_populates="linked_order")


from app.models import observed_position  # noqa: E402,F401
