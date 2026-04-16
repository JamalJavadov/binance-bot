from sqlalchemy import select

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.models.credentials import ApiCredentials
from app.schemas.credentials import ConnectionTestResponse, CredentialsPayload, CredentialsStatus
from app.services.audit import record_audit
from app.services.order_manager import OrderManager

router = APIRouter(prefix="/credentials", tags=["credentials"])


def mask_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


@router.get("", response_model=CredentialsStatus)
async def get_credentials_status(session: AsyncSession = Depends(get_session)) -> CredentialsStatus:
    credentials = (await session.execute(select(ApiCredentials).limit(1))).scalar_one_or_none()
    if credentials is None:
        return CredentialsStatus(has_credentials=False)
    return CredentialsStatus(
        has_credentials=True,
        last_updated=credentials.updated_at,
        masked_api_key=mask_key(credentials.api_key),
    )


@router.post("", response_model=CredentialsStatus)
async def save_credentials(payload: CredentialsPayload, session: AsyncSession = Depends(get_session)) -> CredentialsStatus:
    credentials = (await session.execute(select(ApiCredentials).limit(1))).scalar_one_or_none()
    if credentials is None:
        credentials = ApiCredentials(**payload.model_dump())
        session.add(credentials)
    else:
        credentials.api_key = payload.api_key
        credentials.public_key_pem = payload.public_key_pem
        credentials.private_key_pem = payload.private_key_pem
    await record_audit(session, event_type="CREDENTIALS_SAVED", message="Credentials saved")
    await session.commit()
    await session.refresh(credentials)
    return CredentialsStatus(has_credentials=True, last_updated=credentials.updated_at, masked_api_key=mask_key(credentials.api_key))


@router.delete("", response_model=CredentialsStatus)
async def delete_credentials(session: AsyncSession = Depends(get_session)) -> CredentialsStatus:
    credentials = (await session.execute(select(ApiCredentials).limit(1))).scalar_one_or_none()
    if credentials is not None:
        await session.delete(credentials)
        await record_audit(session, event_type="CREDENTIALS_DELETED", message="Credentials deleted")
        await session.commit()
    return CredentialsStatus(has_credentials=False)


@router.get("/test", response_model=ConnectionTestResponse)
async def test_credentials(request: Request, session: AsyncSession = Depends(get_session)) -> ConnectionTestResponse:
    order_manager: OrderManager = request.app.state.order_manager
    credentials = await order_manager.get_credentials(session)
    if credentials is None:
        raise HTTPException(status_code=400, detail="Credentials not configured")
    try:
        balance = await order_manager.get_balance(session, credentials)
        await record_audit(session, event_type="CREDENTIALS_TESTED", message="Connection test successful")
        await session.commit()
        return ConnectionTestResponse(success=True, balance_usdt=float(balance), message="Connected to Binance Futures")
    except Exception as exc:
        await record_audit(session, event_type="CREDENTIALS_TEST_FAILED", message=str(exc))
        await session.commit()
        return ConnectionTestResponse(success=False, balance_usdt=None, message=str(exc))

