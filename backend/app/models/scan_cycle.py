from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ScanStatus, TriggerType


class ScanCycle(Base):
    __tablename__ = "scan_cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[ScanStatus] = mapped_column(Enum(ScanStatus), default=ScanStatus.RUNNING, nullable=False)
    symbols_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    signals_qualified: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trigger_type: Mapped[TriggerType] = mapped_column(Enum(TriggerType), nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    progress_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    signals = relationship("Signal", back_populates="scan_cycle")
    results = relationship("ScanSymbolResult", back_populates="scan_cycle")
