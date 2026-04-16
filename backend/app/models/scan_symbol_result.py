from typing import Any, Optional

from sqlalchemy import Enum, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ScanSymbolOutcome, SignalDirection


class ScanSymbolResult(Base):
    __tablename__ = "scan_symbol_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_cycle_id: Mapped[int] = mapped_column(ForeignKey("scan_cycles.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[Optional[SignalDirection]] = mapped_column(Enum(SignalDirection))
    outcome: Mapped[ScanSymbolOutcome] = mapped_column(Enum(ScanSymbolOutcome), nullable=False)
    confirmation_score: Mapped[Optional[int]] = mapped_column(Integer)
    final_score: Mapped[Optional[int]] = mapped_column(Integer)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    extra_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason_text: Mapped[Optional[str]] = mapped_column(Text)
    filter_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    scan_cycle = relationship("ScanCycle", back_populates="results")
