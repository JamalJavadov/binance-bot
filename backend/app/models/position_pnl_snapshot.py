from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PositionPnlSnapshot(Base):
    __tablename__ = "position_pnl_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    observed_position_id: Mapped[int] = mapped_column(ForeignKey("observed_positions.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)

    observed_position = relationship("ObservedPosition", back_populates="pnl_snapshots")
