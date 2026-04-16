from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import SignalDirection, SignalStatus


class Signal(TimestampMixin, Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_cycle_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scan_cycles.id"))
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[SignalDirection] = mapped_column(Enum(SignalDirection), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), default="4h", nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    take_profit: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    rr_ratio: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    confirmation_score: Mapped[int] = mapped_column(Integer, nullable=False)
    final_score: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    net_r_multiple: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    estimated_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason_text: Mapped[Optional[str]] = mapped_column(Text)
    swing_origin: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    swing_terminus: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    fib_0786_level: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    current_price_at_signal: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    entry_style: Mapped[Optional[str]] = mapped_column(String(20))
    setup_family: Mapped[Optional[str]] = mapped_column(String(50))
    setup_variant: Mapped[Optional[str]] = mapped_column(String(100))
    market_state: Mapped[Optional[str]] = mapped_column(String(50))
    execution_tier: Mapped[Optional[str]] = mapped_column(String(20))
    score_band: Mapped[Optional[str]] = mapped_column(String(20))
    volatility_band: Mapped[Optional[str]] = mapped_column(String(20))
    stats_bucket_key: Mapped[Optional[str]] = mapped_column(String(255))
    strategy_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[SignalStatus] = mapped_column(Enum(SignalStatus), default=SignalStatus.QUALIFIED, nullable=False)
    extra_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    scan_cycle = relationship("ScanCycle", back_populates="signals")
    orders = relationship("Order", back_populates="signal")
