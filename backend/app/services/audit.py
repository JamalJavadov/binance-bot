from app.models.audit_log import AuditLog
from app.models.enums import AuditLevel


async def record_audit(
    session,
    *,
    event_type: str,
    level: AuditLevel = AuditLevel.INFO,
    message: str | None = None,
    symbol: str | None = None,
    scan_cycle_id: int | None = None,
    signal_id: int | None = None,
    order_id: int | None = None,
    details: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        event_type=event_type,
        level=level,
        message=message,
        symbol=symbol,
        scan_cycle_id=scan_cycle_id,
        signal_id=signal_id,
        order_id=order_id,
        details=details or {},
    )
    session.add(entry)
    await session.flush()
    return entry
