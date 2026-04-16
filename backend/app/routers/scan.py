from sqlalchemy import desc, select

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.models.audit_log import AuditLog
from app.models.enums import TriggerType
from app.models.scan_cycle import ScanCycle
from app.models.scan_symbol_result import ScanSymbolResult
from app.schemas.scan import ScanCycleDetailRead, ScanCycleRead, ScanSymbolResultRead, WorkflowEventRead

router = APIRouter(prefix="/scan", tags=["scan"])


@router.get("/status", response_model=ScanCycleRead | None)
async def latest_scan(session: AsyncSession = Depends(get_session)) -> ScanCycleRead | None:
    cycle = (
        await session.execute(
            select(ScanCycle)
            .where(ScanCycle.trigger_type == TriggerType.AUTO_MODE)
            .order_by(desc(ScanCycle.started_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    return ScanCycleRead.model_validate(cycle) if cycle else None


@router.get("/history", response_model=list[ScanCycleRead])
async def scan_history(session: AsyncSession = Depends(get_session)) -> list[ScanCycleRead]:
    cycles = (
        await session.execute(
            select(ScanCycle)
            .where(ScanCycle.trigger_type == TriggerType.AUTO_MODE)
            .order_by(desc(ScanCycle.started_at))
            .limit(50)
        )
    ).scalars().all()
    return [ScanCycleRead.model_validate(cycle) for cycle in cycles]


@router.get("/{cycle_id}/detail", response_model=ScanCycleDetailRead)
async def scan_cycle_detail(cycle_id: int, session: AsyncSession = Depends(get_session)) -> ScanCycleDetailRead:
    cycle = await session.get(ScanCycle, cycle_id)
    if cycle is None or cycle.trigger_type != TriggerType.AUTO_MODE:
        raise HTTPException(status_code=404, detail="Scan cycle not found")

    results = (
        await session.execute(
            select(ScanSymbolResult).where(ScanSymbolResult.scan_cycle_id == cycle_id)
        )
    ).scalars().all()
    workflow = (
        await session.execute(
            select(AuditLog).where(AuditLog.scan_cycle_id == cycle_id)
        )
    ).scalars().all()
    detail_available = bool(results) or any(entry.event_type == "SCAN_STARTED" for entry in workflow)
    if not detail_available:
        return ScanCycleDetailRead(
            cycle=ScanCycleRead.model_validate(cycle),
            detail_available=False,
            results=[],
            workflow=[],
        )

    ordered_results = sorted(
        results,
        key=lambda item: (
            item.final_score is None,
            -(item.final_score or 0),
            -(item.confirmation_score or 0),
            item.symbol,
        ),
    )
    ordered_workflow = sorted(workflow, key=lambda item: (item.timestamp, item.id))
    return ScanCycleDetailRead(
        cycle=ScanCycleRead.model_validate(cycle),
        detail_available=True,
        results=[ScanSymbolResultRead.model_validate(result) for result in ordered_results],
        workflow=[WorkflowEventRead.model_validate(entry) for entry in ordered_workflow],
    )


@router.get("/{cycle_id}", response_model=ScanCycleRead)
async def scan_detail(cycle_id: int, session: AsyncSession = Depends(get_session)) -> ScanCycleRead:
    cycle = await session.get(ScanCycle, cycle_id)
    if cycle is None or cycle.trigger_type != TriggerType.AUTO_MODE:
        raise HTTPException(status_code=404, detail="Scan cycle not found")
    return ScanCycleRead.model_validate(cycle)
