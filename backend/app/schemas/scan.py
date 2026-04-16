from datetime import datetime

from pydantic import Field, field_validator

from app.models.enums import AuditLevel, ScanStatus, ScanSymbolOutcome, SignalDirection, TriggerType
from app.schemas.common import ORMModel


class ScanCycleRead(ORMModel):
    id: int
    started_at: datetime
    completed_at: datetime | None = None
    status: ScanStatus
    symbols_scanned: int
    candidates_found: int
    signals_qualified: int
    trigger_type: TriggerType
    error_message: str | None = None
    progress_pct: float


class ScanSymbolResultRead(ORMModel):
    symbol: str
    direction: SignalDirection | None = None
    outcome: ScanSymbolOutcome
    confirmation_score: int | None = None
    final_score: int | None = None
    score_breakdown: dict
    extra_context: dict = Field(default_factory=dict)
    reason_text: str | None = None
    filter_reasons: list[str]
    error_message: str | None = None

    @field_validator("extra_context", mode="before")
    @classmethod
    def _coerce_extra_context(cls, value):
        return value or {}


class WorkflowEventRead(ORMModel):
    id: int
    timestamp: datetime
    event_type: str
    level: AuditLevel
    symbol: str | None = None
    message: str | None = None


class ScanCycleDetailRead(ORMModel):
    cycle: ScanCycleRead
    detail_available: bool
    results: list[ScanSymbolResultRead]
    workflow: list[WorkflowEventRead]
