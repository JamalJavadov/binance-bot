from sqlalchemy import text

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.schemas.status import BalanceResponse, HealthStatusResponse, PortfolioSummaryResponse, PositionResponse
from app.services.order_manager import OrderManager

router = APIRouter(prefix="/status", tags=["status"])
account_router = APIRouter(prefix="/account", tags=["account"])


async def _ensure_positions_synced(request: Request, session: AsyncSession) -> None:
    position_observer = request.app.state.position_observer
    if position_observer.last_synced_at is not None:
        return
    await position_observer.sync_positions(session)
    await session.commit()


@router.get("", response_model=HealthStatusResponse)
async def get_status(request: Request, session: AsyncSession = Depends(get_session)) -> HealthStatusResponse:
    db_ok = True
    try:
        await session.execute(text("select 1"))
    except Exception:
        db_ok = False
    gateway = request.app.state.gateway
    binance_reachable = await gateway.ping()
    return HealthStatusResponse(
        backend_ok=True,
        db_ok=db_ok,
        binance_reachable=binance_reachable,
        server_time_offset_ms=gateway.server_time_offset_ms,
    )


@account_router.get("/balance", response_model=BalanceResponse)
async def get_balance(request: Request, session: AsyncSession = Depends(get_session)) -> BalanceResponse:
    order_manager: OrderManager = request.app.state.order_manager
    credentials = await order_manager.get_credentials(session)
    snapshot = await order_manager.get_read_account_snapshot(session, credentials)
    return BalanceResponse(
        asset="USDT",
        balance=float(snapshot.wallet_balance),
        available_balance=float(snapshot.available_balance),
        usable_balance=float(snapshot.usable_balance),
        reserve_balance=float(snapshot.reserve_balance),
    )


@account_router.get("/positions", response_model=list[PositionResponse])
async def get_positions(request: Request, session: AsyncSession = Depends(get_session)) -> list[PositionResponse]:
    await _ensure_positions_synced(request, session)
    return await request.app.state.position_observer.position_rows(session)


@account_router.get("/portfolio-summary", response_model=PortfolioSummaryResponse)
async def get_portfolio_summary(request: Request, session: AsyncSession = Depends(get_session)) -> PortfolioSummaryResponse:
    await _ensure_positions_synced(request, session)
    return await request.app.state.position_observer.portfolio_summary(session)
