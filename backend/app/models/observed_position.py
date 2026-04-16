from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.enums import SignalDirection


class ObservedPosition(TimestampMixin, Base):
    __tablename__ = "observed_positions"
    __table_args__ = (UniqueConstraint("symbol", "position_side", name="uq_observed_positions_symbol_side"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[SignalDirection] = mapped_column(Enum(SignalDirection), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    linked_order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    leverage: Mapped[Optional[int]] = mapped_column(Integer)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    linked_order = relationship("Order", back_populates="observed_positions")
    pnl_snapshots = relationship("PositionPnlSnapshot", back_populates="observed_position")


from app.models import position_pnl_snapshot  # noqa: E402,F401
