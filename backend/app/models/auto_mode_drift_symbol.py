from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AutoModeDriftSymbol(TimestampMixin, Base):
    __tablename__ = "auto_mode_drift_symbols"

    symbol: Mapped[str] = mapped_column(String(50), primary_key=True)
    planned_entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    miss_count: Mapped[int] = mapped_column(nullable=False, default=0)
    last_cancelled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
