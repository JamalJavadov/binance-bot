from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import SignalDirection


class AqrrTradeStat(TimestampMixin, Base):
    __tablename__ = "aqrr_trade_stats"

    bucket_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    setup_family: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[SignalDirection] = mapped_column(Enum(SignalDirection), nullable=False)
    market_state: Mapped[str] = mapped_column(String(50), nullable=False)
    score_band: Mapped[str] = mapped_column(String(20), nullable=False)
    volatility_band: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    closed_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
