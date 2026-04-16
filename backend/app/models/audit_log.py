from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AuditLevel


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(String(50))
    scan_cycle_id: Mapped[Optional[int]] = mapped_column(ForeignKey("scan_cycles.id"))
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"))
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    level: Mapped[AuditLevel] = mapped_column(Enum(AuditLevel), default=AuditLevel.INFO, nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text)
