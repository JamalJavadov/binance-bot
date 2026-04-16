from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import desc, select

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_session
from app.models.enums import ScanStatus, SignalDirection, SignalStatus, TriggerType
from app.models.scan_cycle import ScanCycle
from app.models.signal import Signal
from app.schemas.signal import SignalRead, SignalRecommendationRead, SignalRecommendationsRead, SignalLiveReadinessRead
from app.services.order_manager import AccountSnapshot
from app.services.strategy import PRIMARY_STRATEGY_KEY, PRIMARY_STRATEGY_LABEL
from app.services.settings import get_settings_map

router = APIRouter(prefix="/signals", tags=["signals"])


def _strategy_key_for_signal(_signal: Signal) -> str:
    return PRIMARY_STRATEGY_KEY


def _strategy_label(*, trigger_type: TriggerType | None) -> str | None:
    return PRIMARY_STRATEGY_LABEL


def _signal_extra_context(signal: Signal) -> dict:
    return {
        **(signal.extra_context or {}),
        "entry_style": getattr(signal, "entry_style", None),
        "setup_family": getattr(signal, "setup_family", None),
        "setup_variant": getattr(signal, "setup_variant", None),
        "market_state": getattr(signal, "market_state", None),
        "execution_tier": getattr(signal, "execution_tier", None),
        "score_band": getattr(signal, "score_band", None),
        "volatility_band": getattr(signal, "volatility_band", None),
        "stats_bucket_key": getattr(signal, "stats_bucket_key", None),
        "strategy_context": getattr(signal, "strategy_context", {}) or {},
        "rank_value": None if getattr(signal, "rank_value", None) is None else float(signal.rank_value),
        "net_r_multiple": None if getattr(signal, "net_r_multiple", None) is None else float(signal.net_r_multiple),
        "estimated_cost": None if getattr(signal, "estimated_cost", None) is None else float(signal.estimated_cost),
    }


def _signal_read(signal: Signal, *, trigger_type_override: TriggerType | None = None) -> SignalRead:
    trigger_type = trigger_type_override or getattr(getattr(signal, "scan_cycle", None), "trigger_type", None)
    strategy_key = _strategy_key_for_signal(signal)
    setattr(signal, "scan_trigger_type", trigger_type)
    setattr(signal, "strategy_key", strategy_key)
    setattr(signal, "strategy_label", _strategy_label(trigger_type=trigger_type))
    setattr(signal, "strategy_context", getattr(signal, "strategy_context", None) or {})
    setattr(signal, "extra_context", _signal_extra_context(signal))
    return SignalRead.model_validate(signal)


@router.get("", response_model=list[SignalRead])
async def list_signals(
    status: SignalStatus | None = None,
    cycle_id: int | None = None,
    direction: SignalDirection | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[SignalRead]:
    query = (
        select(Signal)
        .join(ScanCycle, Signal.scan_cycle_id == ScanCycle.id)
        .where(ScanCycle.trigger_type == TriggerType.AUTO_MODE)
        .options(selectinload(Signal.scan_cycle))
        .order_by(desc(Signal.created_at))
    )
    if status:
        query = query.where(Signal.status == status)
    if cycle_id:
        query = query.where(Signal.scan_cycle_id == cycle_id)
    if direction:
        query = query.where(Signal.direction == direction)
    rows = (await session.execute(query)).scalars().all()
    return [_signal_read(row) for row in rows]


@router.get("/recommendations", response_model=SignalRecommendationsRead)
async def list_signal_recommendations(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SignalRecommendationsRead:
    latest_completed_scan = (
        await session.execute(
            select(ScanCycle)
            .where(
                ScanCycle.status == ScanStatus.COMPLETE,
                ScanCycle.trigger_type == TriggerType.AUTO_MODE,
            )
            .order_by(desc(ScanCycle.started_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_completed_scan is None:
        return SignalRecommendationsRead(
            latest_completed_scan_id=None,
            latest_completed_scan_trigger_type=None,
            latest_completed_scan_strategy_key=None,
            latest_completed_scan_strategy_label=None,
            refreshed_at=datetime.now(timezone.utc),
            items=[],
        )

    ranked_signals = (
        await session.execute(
            select(Signal)
            .options(selectinload(Signal.scan_cycle))
            .where(
                Signal.scan_cycle_id == latest_completed_scan.id,
                Signal.status == SignalStatus.QUALIFIED,
            )
            .order_by(
                desc(Signal.rank_value).nullslast(),
                desc(Signal.final_score),
                desc(Signal.confirmation_score),
                Signal.symbol,
            )
            .limit(3)
        )
    ).scalars().all()

    strategy_key = PRIMARY_STRATEGY_KEY
    strategy_label = _strategy_label(trigger_type=latest_completed_scan.trigger_type)
    if not ranked_signals:
        return SignalRecommendationsRead(
            latest_completed_scan_id=latest_completed_scan.id,
            latest_completed_scan_trigger_type=latest_completed_scan.trigger_type,
            latest_completed_scan_strategy_key=strategy_key,
            latest_completed_scan_strategy_label=strategy_label,
            refreshed_at=datetime.now(timezone.utc),
            items=[],
        )

    order_manager = request.app.state.order_manager
    settings_map = await get_settings_map(session)
    credentials = await order_manager.get_credentials(session)
    credentials_available = credentials is not None
    account_snapshot = AccountSnapshot.from_available_balance(
        Decimal("0"),
        reserve_fraction=order_manager.BALANCE_RESERVE_FRACTION,
    )
    account_error: str | None = None
    if credentials_available:
        try:
            account_snapshot = await order_manager.get_read_account_snapshot(session, credentials)
        except Exception as exc:
            account_error = f"Unable to load Binance account balance right now: {exc}"

    try:
        exchange_info = await request.app.state.gateway.read_cached_exchange_info()
        filters_map = request.app.state.gateway.parse_symbol_filters(exchange_info)
    except Exception:
        filters_map = {}

    try:
        mark_prices_map = await request.app.state.gateway.read_cached_mark_prices()
    except Exception:
        mark_prices_map = {}

    leverage_brackets_map = {}
    if credentials_available:
        try:
            leverage_brackets_map = await request.app.state.gateway.read_cached_leverage_brackets(credentials)
        except Exception:
            leverage_brackets_map = {}

    items: list[SignalRecommendationRead] = []
    for rank, signal in enumerate(ranked_signals, start=1):
        live_readiness = await order_manager.get_live_signal_readiness(
            session,
            signal=signal,
            settings_map=settings_map,
            account_snapshot=account_snapshot,
            filters_map=filters_map,
            leverage_brackets_map=leverage_brackets_map,
            mark_prices_map=mark_prices_map,
            credentials_available=credentials_available,
            account_error=account_error,
            use_stop_distance_position_sizing=True,
        )
        items.append(
            SignalRecommendationRead(
                rank=rank,
                signal=_signal_read(signal, trigger_type_override=latest_completed_scan.trigger_type),
                live_readiness=SignalLiveReadinessRead.model_validate(live_readiness),
            )
        )

    return SignalRecommendationsRead(
        latest_completed_scan_id=latest_completed_scan.id,
        latest_completed_scan_trigger_type=latest_completed_scan.trigger_type,
        latest_completed_scan_strategy_key=strategy_key,
        latest_completed_scan_strategy_label=strategy_label,
        refreshed_at=datetime.now(timezone.utc),
        items=items,
    )


@router.get("/{signal_id}", response_model=SignalRead)
async def get_signal(signal_id: int, session: AsyncSession = Depends(get_session)) -> SignalRead:
    signal = (
        await session.execute(
            select(Signal)
            .join(ScanCycle, Signal.scan_cycle_id == ScanCycle.id)
            .options(selectinload(Signal.scan_cycle))
            .where(
                Signal.id == signal_id,
                ScanCycle.trigger_type == TriggerType.AUTO_MODE,
            )
        )
    ).scalar_one_or_none()
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return _signal_read(signal)
