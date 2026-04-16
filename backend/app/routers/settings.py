from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.schemas.settings import SettingsPatch, SettingsResponse
from app.services.audit import record_audit
from app.services.settings import get_settings_map, patch_settings

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsResponse)
async def get_settings(session: AsyncSession = Depends(get_session)) -> SettingsResponse:
    return SettingsResponse(settings=await get_settings_map(session))


@router.patch("", response_model=SettingsResponse)
async def update_settings(
    payload: SettingsPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SettingsResponse:
    try:
        mapping = await patch_settings(session, payload.values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit(session, event_type="SETTINGS_UPDATED", message="Settings updated", details=payload.values)
    await session.commit()
    await request.app.state.scheduler_service.reload()
    return SettingsResponse(settings=mapping)
