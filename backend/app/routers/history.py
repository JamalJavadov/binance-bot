from sqlalchemy import desc, select

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.models.audit_log import AuditLog

router = APIRouter(prefix="/history", tags=["history"])


@router.get("")
async def get_history(session: AsyncSession = Depends(get_session)) -> list[dict]:
    entries = (await session.execute(select(AuditLog).order_by(desc(AuditLog.timestamp)).limit(200))).scalars().all()
    return [
        {
            "id": entry.id,
            "timestamp": entry.timestamp.isoformat(),
            "event_type": entry.event_type,
            "symbol": entry.symbol,
            "message": entry.message,
            "level": entry.level.value,
            "details": entry.details,
        }
        for entry in entries
    ]
