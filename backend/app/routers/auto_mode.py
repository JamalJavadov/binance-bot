from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.schemas.auto_mode import AutoModeStatusRead, AutoModeUpdateRequest
from app.services.audit import record_audit
from app.services.settings import get_settings_map, patch_settings


router = APIRouter(prefix="/auto-mode", tags=["auto-mode"])


@router.get("", response_model=AutoModeStatusRead)
async def get_auto_mode_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AutoModeStatusRead:
    next_cycle_at = request.app.state.scheduler_service.auto_mode_next_run_at()
    return await request.app.state.auto_mode_service.get_status(session, next_cycle_at=next_cycle_at)


@router.patch("", response_model=AutoModeStatusRead)
async def update_auto_mode(
    payload: AutoModeUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AutoModeStatusRead:
    auto_mode_service = request.app.state.auto_mode_service
    scheduler_service = request.app.state.scheduler_service
    current_settings = await get_settings_map(session)
    updates: dict[str, str] = {}
    if payload.enabled is not None:
        updates["auto_mode_enabled"] = str(payload.enabled)
        updates["auto_mode_paused"] = "false"
    intended_enabled = payload.enabled
    if intended_enabled is None:
        intended_enabled = current_settings.get("auto_mode_enabled", "false").lower() == "true"
    if payload.paused is not None:
        if not intended_enabled:
            raise HTTPException(status_code=400, detail="Auto Mode must be enabled before it can be paused or resumed")
        updates["auto_mode_paused"] = str(payload.paused)
    if not updates:
        next_cycle_at = request.app.state.scheduler_service.auto_mode_next_run_at()
        return await request.app.state.auto_mode_service.get_status(session, next_cycle_at=next_cycle_at)

    try:
        updated_settings = await patch_settings(session, updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    enabled_before = current_settings.get("auto_mode_enabled", "false").lower() == "true"
    enabled_after = updated_settings.get("auto_mode_enabled", "false").lower() == "true"
    paused_before = current_settings.get("auto_mode_paused", "false").lower() == "true"
    paused_after = updated_settings.get("auto_mode_paused", "false").lower() == "true"
    should_start_cycle = enabled_after and not paused_after and (not enabled_before or paused_before)
    should_shutdown_auto_mode = payload.enabled is False and (enabled_before or auto_mode_service.running)
    should_pause_auto_mode = payload.paused is True and enabled_after and not paused_before
    should_resume_auto_mode = payload.paused is False and enabled_after and paused_before

    await record_audit(
        session,
        event_type="AUTO_MODE_UPDATED",
        message="Auto Mode settings updated",
        details={
            "enabled": enabled_after,
            "paused": paused_after,
            "signal_schedule": "15m_closed_candle",
            "triggered_immediate_cycle": should_start_cycle,
        },
    )
    await session.commit()

    if should_shutdown_auto_mode:
        await auto_mode_service.shutdown(broadcast_reason="stopped")
    elif should_pause_auto_mode:
        await auto_mode_service.pause(request.app.state.position_observer, broadcast_reason="paused")

    await scheduler_service.reload()
    if should_start_cycle and not should_pause_auto_mode:
        await auto_mode_service.queue_cycle(reason="enabled")
    if should_resume_auto_mode:
        await auto_mode_service.broadcast_state(reason="resumed")

    if not should_shutdown_auto_mode and not should_pause_auto_mode and not should_resume_auto_mode:
        await auto_mode_service.broadcast_state(reason="settings_updated" if enabled_after else "stopped")
    next_cycle_at = scheduler_service.auto_mode_next_run_at()
    return await auto_mode_service.get_status(session, next_cycle_at=next_cycle_at)
