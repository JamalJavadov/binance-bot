import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, desc, select

from app.core.logging import get_logger
from app.models.auto_mode_drift_symbol import AutoModeDriftSymbol
from app.models.enums import AuditLevel, OrderStatus, SignalStatus, TriggerType
from app.models.order import Order
from app.models.scan_cycle import ScanCycle
from app.models.scan_symbol_result import ScanSymbolResult
from app.models.signal import Signal
from app.schemas.auto_mode import AutoModeStatusRead
from app.services.audit import record_audit
from app.services.settings import get_settings_map, resolve_auto_mode_max_entry_distance_fraction
from app.services.strategy.indicators import (
    closes,
    percentage_returns,
    required_15m_candles_for_volatility_shock,
)
from app.services.strategy.aqrr import MARKET_STATE_UNSTABLE, classify_market_state
from app.services.strategy.config import resolve_strategy_config
from app.services.strategy.types import closed_candles, parse_klines


logger = get_logger(__name__)


@dataclass(frozen=True)
class RankedPendingOrder:
    order: Order
    final_score: int
    confirmation_score: int
    rank_value: float = -1.0


@dataclass(frozen=True)
class KillSwitchState:
    active: bool
    reason: str | None
    consecutive_stop_losses: int
    realized_session_pnl: Decimal
    drawdown_fraction: Decimal
    open_risk_plus_loss: Decimal


@dataclass(frozen=True)
class RebalanceSummary:
    opened_order_count: int = 0


@dataclass(frozen=True)
class PendingInvalidationDecision:
    lifecycle_reason: str | None
    cancel_pending: bool = True
    decision_reason: str | None = None
    raw_aqrr_reason: str | None = None
    raw_aqrr_reasons: tuple[str, ...] = ()
    aqrr_rejection_stage: str | None = None


@dataclass(frozen=True)
class EmergencySafetyState:
    active: bool
    reason: str | None = None
    halt_new_entries: bool = False
    cancel_pending_entries: bool = False
    flatten_open_positions: bool = False
    details: dict[str, object] = field(default_factory=dict)


class AutoModeService:
    APPROVED_BY = "AUTO_MODE"
    MAX_ACTIVE_ORDERS = 3
    TOO_FAR_CANCEL_REASON = "setup_state_changed"
    ACTIVE_ORDER_STATUSES = (OrderStatus.SUBMITTING, OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION)
    LIVE_SPREAD_DEGRADATION_CLOSE_COUNT = 2
    DEFAULT_CORRELATION_THRESHOLD = Decimal("0.80")
    MARK_PRICE_ABNORMAL_DEVIATION_THRESHOLD = Decimal("0.015")

    def __init__(self, scanner_service, order_manager, gateway, ws_manager, session_factory, market_health=None) -> None:
        self.scanner_service = scanner_service
        self.order_manager = order_manager
        self.gateway = gateway
        self.ws_manager = ws_manager
        self.session_factory = session_factory
        self.market_health = market_health
        self._cycle_lock = asyncio.Lock()
        self._active_task: asyncio.Task | None = None
        self._queued_task: asyncio.Task | None = None
        self._live_execution_failures: dict[int, int] = {}
        self.last_cycle_started_at: datetime | None = None
        self.last_cycle_completed_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._cycle_lock.locked() or any(
            task is not None and not task.done()
            for task in (self._active_task, self._queued_task)
        )

    def _has_conflicting_cycle_task(self, *, current_task: asyncio.Task | None) -> bool:
        if self._cycle_lock.locked():
            return True
        return any(
            task is not None and not task.done() and task is not current_task
            for task in (self._active_task, self._queued_task)
        )

    async def stop(self) -> bool:
        tasks: list[asyncio.Task] = []
        cancel_requested = False
        for candidate in (self._active_task, self._queued_task):
            if (
                candidate is None
                or candidate.done()
                or candidate is asyncio.current_task()
                or any(candidate is task for task in tasks)
            ):
                continue
            candidate.cancel()
            tasks.append(candidate)
            cancel_requested = True

        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception as exc:
                logger.warning("auto_mode.stop_task_failed", error=str(exc))

        if self._active_task is not None and self._active_task.done():
            self._active_task = None
        if self._queued_task is not None and self._queued_task.done():
            self._queued_task = None
        return cancel_requested

    async def _mode_is_enabled(self, session) -> bool:
        return self._is_cycle_enabled(await get_settings_map(session))

    async def _record_cycle_cancelled(
        self,
        session,
        *,
        reason: str,
        cancel_reason: str,
        scan_cycle_id: int | None = None,
    ) -> None:
        await record_audit(
            session,
            event_type="AUTO_MODE_CYCLE_CANCELLED",
            level=AuditLevel.INFO,
            message="Auto Mode cycle cancelled",
            scan_cycle_id=scan_cycle_id,
            details={"reason": reason, "cancel_reason": cancel_reason},
        )
        await session.commit()

    async def _shutdown_orders(self, session, *, reason: str) -> tuple[int, int]:
        managed_orders = await self._active_orders(session, approved_by=self.APPROVED_BY)
        for order in managed_orders:
            try:
                await self.order_manager.sync_order(session, order)
            except Exception as exc:
                logger.warning("auto_mode.shutdown_sync_failed", order_id=order.id, symbol=order.symbol, error=str(exc))

        cancelled_orders = 0
        monitored_positions = 0
        for order in managed_orders:
            try:
                if order.status == OrderStatus.ORDER_PLACED:
                    await self.order_manager.cancel_order(session, order_id=order.id, reason=reason)
                    cancelled_orders += 1
                elif order.status == OrderStatus.IN_POSITION:
                    monitored_positions += 1
            except Exception as exc:
                logger.warning("auto_mode.shutdown_order_failed", order_id=order.id, symbol=order.symbol, error=str(exc))

        return cancelled_orders, monitored_positions

    async def shutdown(self, *, broadcast_reason: str) -> None:
        cancelled_cycle = await self.stop()
        async with self.session_factory() as session:
            await self.order_manager.reconcile_managed_orders(session, approved_by=self.APPROVED_BY)
            cancelled_orders, monitored_positions = await self._shutdown_orders(session, reason="viability_lost")
            await record_audit(
                session,
                event_type="AUTO_MODE_STOPPED",
                level=AuditLevel.INFO,
                message="Auto Mode stopped",
                details={
                    "broadcast_reason": broadcast_reason,
                    "cancelled_cycle": cancelled_cycle,
                    "cancelled_orders": cancelled_orders,
                    "monitored_positions": monitored_positions,
                },
            )
            await session.commit()

        self.last_cycle_completed_at = datetime.now(timezone.utc)
        await self.broadcast_state(reason=broadcast_reason)

    async def pause(self, position_observer, *, broadcast_reason: str) -> None:
        cancelled_cycle = await self.stop()
        async with self.session_factory() as session:
            await self.order_manager.reconcile_managed_orders(session, approved_by=self.APPROVED_BY)
            await position_observer.sync_positions(session)
            managed_orders = await self._active_orders(session, approved_by=self.APPROVED_BY)
            await record_audit(
                session,
                event_type="AUTO_MODE_PAUSED",
                level=AuditLevel.INFO,
                message="Auto Mode paused",
                details={
                    "broadcast_reason": broadcast_reason,
                    "cancelled_cycle": cancelled_cycle,
                    "managed_order_count": len(managed_orders),
                },
            )
            await session.commit()

        self.last_cycle_completed_at = datetime.now(timezone.utc)
        await self.broadcast_state(reason=broadcast_reason)

    async def broadcast_state(self, *, reason: str) -> None:
        await self.ws_manager.broadcast(
            "auto_mode_state_change",
            {
                "running": self.running,
                "reason": reason,
            },
        )

    async def queue_cycle(self, *, reason: str) -> bool:
        if self._has_conflicting_cycle_task(current_task=None):
            return False
        self._queued_task = asyncio.create_task(self.run_cycle(reason=reason))
        return True

    async def _latest_auto_mode_cycle(self, session) -> ScanCycle | None:
        return (
            await session.execute(
                select(ScanCycle)
                .where(ScanCycle.trigger_type == TriggerType.AUTO_MODE)
                .order_by(desc(ScanCycle.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _active_orders(self, session, *, approved_by: str | None = None) -> list[Order]:
        query = select(Order).where(Order.status.in_(self.ACTIVE_ORDER_STATUSES))
        if approved_by is not None:
            query = query.where(Order.approved_by == approved_by)
        return (await session.execute(query)).scalars().all()

    @staticmethod
    def _session_start(now: datetime) -> datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    async def _session_closed_auto_orders(self, session, *, now: datetime) -> list[Order]:
        session_start = self._session_start(now)
        return (
            await session.execute(
                select(Order)
                .where(
                    Order.approved_by == self.APPROVED_BY,
                    Order.closed_at.is_not(None),
                    Order.closed_at >= session_start,
                )
                .order_by(desc(Order.closed_at), desc(Order.id))
            )
        ).scalars().all()

    async def _kill_switch_state(
        self,
        session,
        *,
        settings_map: dict[str, str],
        account_snapshot,
        active_auto_orders: list[Order],
    ) -> KillSwitchState:
        config = resolve_strategy_config(settings_map)
        now = datetime.now(timezone.utc)
        closed_orders = await self._session_closed_auto_orders(session, now=now)
        consecutive_stop_losses = 0
        for order in closed_orders:
            if order.close_type == "SL" and order.status == OrderStatus.CLOSED_LOSS:
                consecutive_stop_losses += 1
                continue
            break

        realized_session_pnl = sum(
            (Decimal(order.realized_pnl or 0) for order in closed_orders),
            start=Decimal("0"),
        )
        realized_session_loss = abs(min(realized_session_pnl, Decimal("0")))
        session_start_equity = max(account_snapshot.wallet_balance + realized_session_loss, Decimal("0"))
        drawdown_fraction = (
            realized_session_loss / session_start_equity
            if session_start_equity > 0
            else Decimal("0")
        )
        active_risk_usdt = sum(
            (
                Decimal(order.risk_usdt_at_stop or 0)
                for order in active_auto_orders
                if order.status == OrderStatus.IN_POSITION
            ),
            start=Decimal("0"),
        )
        open_risk_plus_loss = active_risk_usdt + realized_session_loss
        safe_tolerance = account_snapshot.wallet_balance * config.max_portfolio_risk_fraction

        reason: str | None = None
        if consecutive_stop_losses >= config.kill_switch_consecutive_stop_losses:
            reason = "consecutive_stop_losses"
        elif drawdown_fraction >= config.kill_switch_daily_drawdown_fraction:
            reason = "daily_drawdown"
        elif safe_tolerance > 0 and open_risk_plus_loss > safe_tolerance:
            reason = "session_loss_plus_open_risk"
        return KillSwitchState(
            active=reason is not None,
            reason=reason,
            consecutive_stop_losses=consecutive_stop_losses,
            realized_session_pnl=realized_session_pnl,
            drawdown_fraction=drawdown_fraction,
            open_risk_plus_loss=open_risk_plus_loss,
        )

    async def _actionable_signals_for_cycle(self, session, *, cycle_id: int) -> list[Signal]:
        return (
            await session.execute(
                select(Signal)
                .where(
                    Signal.scan_cycle_id == cycle_id,
                    Signal.status == SignalStatus.QUALIFIED,
                )
                .order_by(
                    desc(Signal.rank_value).nullslast(),
                    desc(Signal.final_score),
                    desc(Signal.confirmation_score),
                    Signal.symbol,
                )
            )
        ).scalars().all()

    async def _sync_existing_orders(self, session) -> None:
        await self.order_manager.reconcile_managed_orders(session, approved_by=self.APPROVED_BY)
        for order in await self._active_orders(session, approved_by=self.APPROVED_BY):
            if order.status == OrderStatus.SUBMITTING:
                continue
            await self.order_manager.sync_order(session, order)

    async def _ranked_pending_orders(self, session, *, orders: list[Order]) -> list[RankedPendingOrder]:
        signal_ids = [order.signal_id for order in orders if order.signal_id is not None]
        signals_by_id: dict[int, Signal] = {}
        if signal_ids:
            signals = (await session.execute(select(Signal).where(Signal.id.in_(signal_ids)))).scalars().all()
            signals_by_id = {signal.id: signal for signal in signals}

        ranked_orders: list[RankedPendingOrder] = []
        for order in orders:
            signal = signals_by_id.get(order.signal_id)
            ranked_orders.append(
                RankedPendingOrder(
                    order=order,
                    final_score=signal.final_score if signal is not None else -1,
                    confirmation_score=signal.confirmation_score if signal is not None else -1,
                    rank_value=(
                        float(signal.rank_value)
                        if signal is not None and signal.rank_value is not None
                        else float(signal.final_score)
                        if signal is not None
                        else -1.0
                    ),
                )
            )
        return ranked_orders

    @staticmethod
    def _is_enabled(settings_map: dict[str, str]) -> bool:
        return settings_map.get("auto_mode_enabled", "false").lower() == "true"

    @staticmethod
    def _is_paused(settings_map: dict[str, str]) -> bool:
        return settings_map.get("auto_mode_paused", "false").lower() == "true"

    @classmethod
    def _is_cycle_enabled(cls, settings_map: dict[str, str]) -> bool:
        return cls._is_enabled(settings_map) and not cls._is_paused(settings_map)

    @staticmethod
    def _score_tuple(
        *,
        rank_value: float | Decimal | None,
        final_score: int | None,
        confirmation_score: int | None,
    ) -> tuple[float, int, int]:
        return (
            float(rank_value) if rank_value is not None else float(final_score or 0),
            final_score or 0,
            confirmation_score or 0,
        )

    @staticmethod
    def _entry_distance_pct(*, mark_price: Decimal, entry_price: Decimal) -> Decimal | None:
        if entry_price <= 0:
            return None
        return abs(mark_price - entry_price) / entry_price

    @staticmethod
    def _decimal_from_payload(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        return format(value.normalize(), "f") if value != 0 else "0"

    async def _fresh_mark_payload(
        self,
        *,
        symbol: str,
        fallback_map: dict[str, dict] | None = None,
    ) -> dict | None:
        try:
            return await self.gateway.mark_price(symbol)
        except Exception as exc:
            logger.warning("auto_mode.mark_price_refresh_failed", symbol=symbol, error=str(exc))
            return None if fallback_map is None else fallback_map.get(symbol)

    def _entry_distance_message(
        self,
        *,
        symbol: str,
        mark_price: Decimal,
        entry_price: Decimal,
        distance_pct: Decimal,
        max_distance_pct: Decimal,
    ) -> str:
        return (
            f"{symbol} skipped because live mark {mark_price:.8f} is "
            f"{(distance_pct * Decimal('100')):.2f}% away from entry {entry_price:.8f}. "
            f"Auto Mode only keeps pending orders within {(max_distance_pct * Decimal('100')):.2f}% of entry."
        )

    @staticmethod
    def _signal_reason_context(signal: Signal) -> dict[str, object]:
        extra_context = dict(getattr(signal, "extra_context", {}) or {})
        raw_aqrr_reasons = [
            str(reason)
            for reason in (extra_context.get("aqrr_raw_rejection_reasons") or extra_context.get("raw_aqrr_reasons") or [])
            if isinstance(reason, str) and reason.strip()
        ]
        raw_aqrr_reason = str(
            extra_context.get("aqrr_raw_rejection_reason")
            or extra_context.get("raw_aqrr_reason")
            or ""
        ).strip() or None
        if raw_aqrr_reason is None and raw_aqrr_reasons:
            raw_aqrr_reason = raw_aqrr_reasons[0]

        payload: dict[str, object] = {}
        if raw_aqrr_reason is not None:
            payload["raw_aqrr_reason"] = raw_aqrr_reason
        if raw_aqrr_reasons:
            payload["raw_aqrr_reasons"] = raw_aqrr_reasons
        aqrr_rejection_stage = str(extra_context.get("aqrr_rejection_stage") or "").strip() or None
        if aqrr_rejection_stage is not None:
            payload["aqrr_rejection_stage"] = aqrr_rejection_stage
        setup_family = getattr(signal, "setup_family", None) or extra_context.get("setup_family")
        if setup_family:
            payload["setup_family"] = str(setup_family)
        entry_style = getattr(signal, "entry_style", None) or extra_context.get("entry_style")
        if entry_style:
            payload["entry_style"] = str(entry_style)
        return payload

    async def _record_order_skipped(
        self,
        session,
        *,
        signal: Signal,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        payload = dict(details or {})
        for key, value in self._signal_reason_context(signal).items():
            payload.setdefault(key, value)
        await record_audit(
            session,
            event_type="AUTO_MODE_ORDER_SKIPPED",
            level=AuditLevel.WARNING,
            message=message,
            symbol=signal.symbol,
            scan_cycle_id=signal.scan_cycle_id,
            signal_id=signal.id,
            details=payload,
        )
        await session.commit()

    async def _load_drift_symbols(self, session) -> list[AutoModeDriftSymbol]:
        return (
            await session.execute(
                select(AutoModeDriftSymbol).order_by(AutoModeDriftSymbol.symbol)
            )
        ).scalars().all()

    async def _delete_drift_symbol(self, session, *, symbol: str) -> None:
        await session.execute(delete(AutoModeDriftSymbol).where(AutoModeDriftSymbol.symbol == symbol.upper()))

    async def _upsert_drift_symbol(
        self,
        session,
        *,
        symbol: str,
        planned_entry_price: Decimal,
        scan_cycle_id: int | None,
    ) -> None:
        normalized_symbol = symbol.upper()
        drift_symbol = await session.get(AutoModeDriftSymbol, normalized_symbol)
        now = datetime.now(timezone.utc)
        if drift_symbol is None:
            drift_symbol = AutoModeDriftSymbol(
                symbol=normalized_symbol,
                planned_entry_price=planned_entry_price,
                miss_count=0,
                last_cancelled_at=now,
            )
            session.add(drift_symbol)
        else:
            drift_symbol.planned_entry_price = planned_entry_price
            drift_symbol.miss_count = 0
            drift_symbol.last_cancelled_at = now
        await record_audit(
            session,
            event_type="AUTO_MODE_DRIFT_TRACKED",
            level=AuditLevel.INFO,
            message=f"{normalized_symbol} added to drift requalification tracking",
            symbol=normalized_symbol,
            scan_cycle_id=scan_cycle_id,
            details={
                "reason": "drift_cancelled",
                "planned_entry_price": self._decimal_string(planned_entry_price),
            },
        )

    async def _increment_drift_miss(
        self,
        session,
        *,
        drift_symbol: AutoModeDriftSymbol,
        scan_cycle_id: int | None,
        reason: str,
        mark_price: Decimal | None = None,
        distance_pct: Decimal | None = None,
    ) -> None:
        drift_symbol.miss_count += 1
        details: dict[str, str | int] = {
            "reason": reason,
            "miss_count": drift_symbol.miss_count,
            "planned_entry_price": self._decimal_string(Decimal(drift_symbol.planned_entry_price)),
        }
        if mark_price is not None:
            details["mark_price"] = self._decimal_string(mark_price)
        if distance_pct is not None:
            details["distance_pct"] = self._decimal_string(distance_pct)

        if drift_symbol.miss_count >= 3:
            await record_audit(
                session,
                event_type="AUTO_MODE_DRIFT_EXPIRED",
                level=AuditLevel.INFO,
                message=f"{drift_symbol.symbol} removed from drift requalification tracking",
                symbol=drift_symbol.symbol,
                scan_cycle_id=scan_cycle_id,
                details=details,
            )
            await self._delete_drift_symbol(session, symbol=drift_symbol.symbol)
            return

        await record_audit(
            session,
            event_type="AUTO_MODE_DRIFT_RECHECK_MISSED",
            level=AuditLevel.INFO,
            message=f"{drift_symbol.symbol} did not re-qualify this cycle",
            symbol=drift_symbol.symbol,
            scan_cycle_id=scan_cycle_id,
            details=details,
        )

    async def _ready_drift_symbols_for_cycle(self, session, *, scan_cycle_id: int | None = None) -> list[str]:
        drift_symbols = await self._load_drift_symbols(session)
        if not drift_symbols:
            return []

        max_entry_distance_pct = resolve_auto_mode_max_entry_distance_fraction(await get_settings_map(session))
        mark_prices_map = await self.gateway.mark_prices()
        ready_symbols: list[str] = []
        for drift_symbol in drift_symbols:
            mark_payload = await self._fresh_mark_payload(
                symbol=drift_symbol.symbol,
                fallback_map=mark_prices_map,
            )
            mark_price = self._decimal_from_payload(None if mark_payload is None else mark_payload.get("markPrice"))
            if mark_price is None:
                await self._increment_drift_miss(
                    session,
                    drift_symbol=drift_symbol,
                    scan_cycle_id=scan_cycle_id,
                    reason="drift_requalification_mark_unavailable",
                )
                continue
            distance_pct = self._entry_distance_pct(
                mark_price=mark_price,
                entry_price=Decimal(drift_symbol.planned_entry_price),
            )
            if distance_pct is not None and distance_pct <= max_entry_distance_pct:
                ready_symbols.append(drift_symbol.symbol)
                continue
            await self._increment_drift_miss(
                session,
                drift_symbol=drift_symbol,
                scan_cycle_id=scan_cycle_id,
                reason="drift_requalification_still_far",
                mark_price=mark_price,
                distance_pct=distance_pct,
            )
        return ready_symbols

    async def _process_drift_requalification_results(
        self,
        session,
        *,
        scan_cycle_id: int,
        ready_symbols: list[str],
    ) -> None:
        if not ready_symbols:
            return

        qualifying_signals = (
            await session.execute(
                select(Signal).where(
                    Signal.scan_cycle_id == scan_cycle_id,
                    Signal.symbol.in_(ready_symbols),
                    Signal.status == SignalStatus.QUALIFIED,
                )
            )
        ).scalars().all()
        qualifying_symbols = {signal.symbol.upper() for signal in qualifying_signals}

        for symbol in ready_symbols:
            drift_symbol = await session.get(AutoModeDriftSymbol, symbol.upper())
            if drift_symbol is None:
                continue
            if symbol.upper() in qualifying_symbols:
                drift_symbol.miss_count = 0
                await record_audit(
                    session,
                    event_type="AUTO_MODE_DRIFT_REQUALIFIED",
                    level=AuditLevel.INFO,
                    message=f"{symbol.upper()} re-qualified after drift cancellation",
                    symbol=symbol.upper(),
                    scan_cycle_id=scan_cycle_id,
                    details={"reason": "drift_requalified"},
                )
                continue
            await self._increment_drift_miss(
                session,
                drift_symbol=drift_symbol,
                scan_cycle_id=scan_cycle_id,
                reason="drift_requalification_failed",
            )

    @classmethod
    def _pending_order_sort_key(cls, entry: RankedPendingOrder) -> tuple[float, int, int, int]:
        return (
            cls._score_tuple(
                rank_value=entry.rank_value,
                final_score=entry.final_score,
                confirmation_score=entry.confirmation_score,
            )[0],
            entry.final_score,
            entry.confirmation_score,
            -entry.order.id,
        )

    @classmethod
    def _weakest_pending_order(cls, entries: list[RankedPendingOrder]) -> RankedPendingOrder | None:
        if not entries:
            return None
        return min(entries, key=cls._pending_order_sort_key)

    @classmethod
    def _signal_is_strictly_better(cls, signal: Signal, incumbent: RankedPendingOrder) -> bool:
        return cls._score_tuple(
            rank_value=signal.rank_value,
            final_score=signal.final_score,
            confirmation_score=signal.confirmation_score,
        ) > cls._score_tuple(
            rank_value=incumbent.rank_value,
            final_score=incumbent.final_score,
            confirmation_score=incumbent.confirmation_score,
        )

    @staticmethod
    def _correlation(left: list[float], right: list[float]) -> float:
        size = min(len(left), len(right))
        if size < 3:
            return 0.0
        left_values = left[-size:]
        right_values = right[-size:]
        left_mean = sum(left_values) / size
        right_mean = sum(right_values) / size
        left_diff = [value - left_mean for value in left_values]
        right_diff = [value - right_mean for value in right_values]
        numerator = sum(a * b for a, b in zip(left_diff, right_diff))
        left_scale = sum(a * a for a in left_diff)
        right_scale = sum(b * b for b in right_diff)
        if left_scale <= 0 or right_scale <= 0:
            return 0.0
        return numerator / ((left_scale * right_scale) ** 0.5)

    async def _returns_1h(self, *, symbol: str, cache: dict[str, list[float]]) -> list[float]:
        normalized_symbol = symbol.upper()
        if normalized_symbol in cache:
            return cache[normalized_symbol]
        candles_1h = closed_candles(parse_klines(await self.gateway.klines(normalized_symbol, "1h", 260), symbol=normalized_symbol))
        returns_1h = percentage_returns(closes(candles_1h)[-73:])
        cache[normalized_symbol] = returns_1h
        return returns_1h

    async def _load_closed_candles(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list:
        if hasattr(self.gateway, "klines_history"):
            raw = await self.gateway.klines_history(symbol, interval, limit)
        else:
            raw = await self.gateway.klines(symbol, interval, limit)
        return closed_candles(parse_klines(raw, symbol=symbol))

    @staticmethod
    def _pending_setup_state_matches_signal(*, order: Order, signal: Signal) -> bool:
        order_setup_family = str(getattr(order, "setup_family", "") or "").strip()
        signal_setup_family = str(getattr(signal, "setup_family", "") or "").strip()
        if not order_setup_family or not signal_setup_family or order_setup_family != signal_setup_family:
            return False
        order_setup_variant = str(getattr(order, "setup_variant", "") or "").strip()
        signal_setup_variant = str(getattr(signal, "setup_variant", "") or "").strip()
        if order_setup_variant and signal_setup_variant and order_setup_variant != signal_setup_variant:
            return False
        order_entry_style = str(getattr(order, "entry_style", "") or "").strip()
        signal_entry_style = str(getattr(signal, "entry_style", "") or "").strip()
        if order_entry_style and signal_entry_style and order_entry_style != signal_entry_style:
            return False
        return True

    async def _open_position_correlation_conflict(
        self,
        *,
        signal: Signal,
        live_orders: list[Order],
        correlation_threshold: Decimal,
        returns_cache: dict[str, list[float]],
    ) -> dict[str, str] | None:
        if not live_orders:
            return None
        signal_context = dict(getattr(signal, "extra_context", {}) or {})
        if not (
            signal_context.get("strategy_key")
            or signal_context.get("cluster") is not None
            or signal_context.get("btc_beta_correlation") is not None
        ):
            return {
                "reason": "correlation_guard_unavailable",
                "guard_failure": "missing_signal_context",
            }
        candidate_returns = await self._returns_1h(symbol=signal.symbol, cache=returns_cache)
        if len(candidate_returns) < 3:
            return {
                "reason": "correlation_guard_unavailable",
                "guard_failure": "insufficient_candidate_returns",
                "returns_count": str(len(candidate_returns)),
            }
        for live_order in live_orders:
            if live_order.direction != signal.direction:
                continue
            live_returns = await self._returns_1h(symbol=live_order.symbol, cache=returns_cache)
            if len(live_returns) < 3:
                return {
                    "reason": "correlation_guard_unavailable",
                    "guard_failure": "insufficient_live_returns",
                    "conflict_symbol": live_order.symbol,
                    "returns_count": str(len(live_returns)),
                }
            correlation = self._correlation(candidate_returns, live_returns)
            if correlation < 0:
                continue
            if abs(correlation) <= float(correlation_threshold):
                continue
            return {
                "conflict_symbol": live_order.symbol,
                "correlation": f"{correlation:.4f}",
                "correlation_threshold": f"{float(correlation_threshold):.4f}",
            }
        return None

    async def _pending_open_correlation_conflict(
        self,
        *,
        order: Order,
        live_orders: list[Order],
        correlation_threshold: Decimal,
        returns_cache: dict[str, list[float]],
    ) -> dict[str, str] | None:
        if order.status != OrderStatus.ORDER_PLACED or not live_orders:
            return None
        strategy_context = dict(getattr(order, "strategy_context", {}) or {})
        if not (getattr(order, "setup_family", None) or strategy_context.get("setup_family")):
            return None
        candidate_returns = await self._returns_1h(symbol=order.symbol, cache=returns_cache)
        if len(candidate_returns) < 3:
            return {
                "reason": "correlation_guard_unavailable",
                "guard_failure": "insufficient_pending_returns",
                "returns_count": str(len(candidate_returns)),
            }
        for live_order in live_orders:
            if live_order.direction != order.direction:
                continue
            if live_order.symbol.upper() == order.symbol.upper():
                continue
            live_returns = await self._returns_1h(symbol=live_order.symbol, cache=returns_cache)
            if len(live_returns) < 3:
                return {
                    "reason": "correlation_guard_unavailable",
                    "guard_failure": "insufficient_live_returns",
                    "conflict_symbol": live_order.symbol,
                    "returns_count": str(len(live_returns)),
                }
            correlation = self._correlation(candidate_returns, live_returns)
            if correlation < 0:
                continue
            if abs(correlation) <= float(correlation_threshold):
                continue
            return {
                "conflict_symbol": live_order.symbol,
                "correlation": f"{correlation:.4f}",
                "correlation_threshold": f"{float(correlation_threshold):.4f}",
            }
        return None

    async def _user_stream_health_state(self, session, *, credentials) -> dict[str, object]:
        if hasattr(self.order_manager, "user_data_stream_health"):
            try:
                payload = await self.order_manager.user_data_stream_health(session, credentials)
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                logger.warning("auto_mode.user_stream_health_check_failed", error=str(exc))
                return {
                    "healthy": False,
                    "required": True,
                    "reason": "health_check_failed",
                    "error": str(exc),
                }
        return {
            "healthy": True,
            "required": False,
            "mode": "polling_fallback",
        }

    async def _order_update_integrity_state(self, session) -> dict[str, object]:
        if hasattr(self.order_manager, "order_update_integrity_state"):
            try:
                payload = await self.order_manager.order_update_integrity_state(session)
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                logger.warning("auto_mode.order_update_integrity_check_failed", error=str(exc))
                return {
                    "healthy": False,
                    "failure_count": 1,
                    "threshold": 1,
                    "reason": "integrity_check_failed",
                    "error": str(exc),
                }
        return {
            "healthy": True,
            "failure_count": 0,
            "threshold": 0,
        }

    async def _exchange_risk_state(self) -> dict[str, object]:
        state_callable = None
        if hasattr(self.gateway, "risk_error_state"):
            state_callable = self.gateway.risk_error_state
        elif hasattr(self.order_manager, "exchange_risk_error_state"):
            state_callable = self.order_manager.exchange_risk_error_state
        if state_callable is None:
            return {
                "healthy": True,
                "risk_error_streak": 0,
                "threshold": 0,
            }
        payload = state_callable()
        if asyncio.iscoroutine(payload):
            payload = await payload
        if isinstance(payload, dict):
            return payload
        return {
            "healthy": True,
            "risk_error_streak": 0,
            "threshold": 0,
        }

    @staticmethod
    def _active_emergency_symbols(*, active_auto_orders: list[Order]) -> list[str]:
        return sorted(
            {
                str(order.symbol).upper()
                for order in active_auto_orders
                if order.status in {OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION}
            }
        )

    async def _mark_price_abnormality_state(
        self,
        *,
        active_auto_orders: list[Order],
    ) -> dict[str, object]:
        symbols = self._active_emergency_symbols(active_auto_orders=active_auto_orders)
        if not symbols:
            return {
                "healthy": True,
                "checked_symbols": 0,
            }
        try:
            mark_prices_map = await self.gateway.mark_prices()
        except Exception as exc:
            logger.warning("auto_mode.mark_price_abnormality_check_failed", error=str(exc))
            return {
                "healthy": True,
                "checked_symbols": len(symbols),
                "mark_price_data_unavailable": True,
                "error": str(exc),
            }

        threshold = self.MARK_PRICE_ABNORMAL_DEVIATION_THRESHOLD
        issues: list[dict[str, str]] = []
        missing_symbols: list[str] = []
        for symbol in symbols:
            payload = mark_prices_map.get(symbol)
            if not isinstance(payload, dict):
                missing_symbols.append(symbol)
                continue
            mark_price = self._decimal_from_payload(payload.get("markPrice"))
            if mark_price is None or mark_price <= 0:
                issues.append(
                    {
                        "symbol": symbol,
                        "issue": "mark_price_invalid",
                        "mark_price": str(payload.get("markPrice")),
                    }
                )
                continue
            index_price = self._decimal_from_payload(payload.get("indexPrice"))
            if index_price is None or index_price <= 0:
                continue
            deviation = abs(mark_price - index_price) / index_price
            if deviation <= threshold:
                continue
            issues.append(
                {
                    "symbol": symbol,
                    "issue": "mark_price_deviation_exceeded",
                    "mark_price": self._decimal_string(mark_price),
                    "index_price": self._decimal_string(index_price),
                    "deviation_pct": self._decimal_string(deviation * Decimal("100")),
                    "threshold_pct": self._decimal_string(threshold * Decimal("100")),
                }
            )

        if not issues:
            return {
                "healthy": True,
                "checked_symbols": len(symbols),
                "missing_mark_price_symbols": missing_symbols,
            }
        return {
            "healthy": False,
            "checked_symbols": len(symbols),
            "affected_symbols": issues,
            "threshold_pct": self._decimal_string(threshold * Decimal("100")),
            "missing_mark_price_symbols": missing_symbols,
        }

    async def _suspension_or_delisting_state(
        self,
        *,
        active_auto_orders: list[Order],
    ) -> dict[str, object]:
        symbols = self._active_emergency_symbols(active_auto_orders=active_auto_orders)
        if not symbols:
            return {
                "healthy": True,
                "checked_symbols": 0,
            }

        try:
            exchange_info = await self.gateway.exchange_info()
        except Exception as exc:
            logger.warning("auto_mode.suspension_delisting_check_failed", error=str(exc))
            return {
                "healthy": True,
                "checked_symbols": len(symbols),
                "exchange_info_unavailable": True,
                "error": str(exc),
            }

        symbol_rows = {
            str(item.get("symbol")).upper(): item
            for item in exchange_info.get("symbols", [])
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        }
        affected_symbols: list[dict[str, str]] = []
        missing_symbols: list[str] = []
        for symbol in symbols:
            symbol_row = symbol_rows.get(symbol)
            if symbol_row is None:
                missing_symbols.append(symbol)
                continue
            status = str(symbol_row.get("status") or "TRADING").upper()
            contract_status = str(symbol_row.get("contractStatus") or status or "TRADING").upper()
            if status == "TRADING" and contract_status == "TRADING":
                continue
            affected_symbols.append(
                {
                    "symbol": symbol,
                    "status": status,
                    "contract_status": contract_status,
                    "issue": "symbol_not_trading",
                }
            )

        if not affected_symbols:
            return {
                "healthy": True,
                "checked_symbols": len(symbols),
                "missing_exchange_info_symbols": missing_symbols,
            }
        return {
            "healthy": False,
            "checked_symbols": len(symbols),
            "affected_symbols": affected_symbols,
            "missing_exchange_info_symbols": missing_symbols,
        }

    def _materially_unsafe_account_state_details(
        self,
        *,
        account_snapshot,
        active_auto_orders: list[Order],
    ) -> dict[str, object] | None:
        live_order_count = len([order for order in active_auto_orders if order.status == OrderStatus.IN_POSITION])
        active_order_count = len([order for order in active_auto_orders if order.status in {OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION}])
        wallet_balance = Decimal(account_snapshot.wallet_balance)
        available_balance = Decimal(account_snapshot.available_balance)
        total_initial_margin = Decimal(getattr(account_snapshot, "total_initial_margin", Decimal("0")) or Decimal("0"))
        total_position_initial_margin = Decimal(getattr(account_snapshot, "total_position_initial_margin", Decimal("0")) or Decimal("0"))

        unsafe_reason: str | None = None
        if available_balance < 0:
            unsafe_reason = "negative_available_balance"
        elif wallet_balance <= 0 and active_order_count > 0:
            unsafe_reason = "zero_or_negative_wallet_balance_with_active_orders"
        elif wallet_balance > 0 and total_position_initial_margin > wallet_balance * Decimal("1.05"):
            unsafe_reason = "position_margin_exceeds_wallet"
        elif wallet_balance > 0 and total_initial_margin > wallet_balance * Decimal("1.10"):
            unsafe_reason = "initial_margin_exceeds_wallet"

        if unsafe_reason is None:
            return None
        return {
            "reason": unsafe_reason,
            "wallet_balance": self._decimal_string(wallet_balance),
            "available_balance": self._decimal_string(available_balance),
            "total_initial_margin": self._decimal_string(total_initial_margin),
            "total_position_initial_margin": self._decimal_string(total_position_initial_margin),
            "live_order_count": live_order_count,
            "active_order_count": active_order_count,
        }

    async def _emergency_safety_state(
        self,
        session,
        *,
        active_auto_orders: list[Order],
        account_snapshot,
    ) -> EmergencySafetyState:
        credentials = await self.order_manager.get_credentials(session)
        live_order_count = len([order for order in active_auto_orders if order.status == OrderStatus.IN_POSITION])

        stream_health = await self._user_stream_health_state(session, credentials=credentials)
        if bool(stream_health.get("required")) and not bool(stream_health.get("healthy", True)):
            return EmergencySafetyState(
                active=True,
                reason="user_stream_unreliable",
                halt_new_entries=True,
                cancel_pending_entries=True,
                flatten_open_positions=live_order_count > 0,
                details=stream_health,
            )

        integrity_state = await self._order_update_integrity_state(session)
        if not bool(integrity_state.get("healthy", True)):
            return EmergencySafetyState(
                active=True,
                reason="order_update_integrity_broken",
                halt_new_entries=True,
                cancel_pending_entries=True,
                flatten_open_positions=live_order_count > 0,
                details=integrity_state,
            )

        exchange_risk_state = await self._exchange_risk_state()
        if not bool(exchange_risk_state.get("healthy", True)):
            return EmergencySafetyState(
                active=True,
                reason="repeated_exchange_risk_errors",
                halt_new_entries=True,
                cancel_pending_entries=True,
                flatten_open_positions=live_order_count > 0,
                details=exchange_risk_state,
            )

        mark_price_state = await self._mark_price_abnormality_state(active_auto_orders=active_auto_orders)
        if not bool(mark_price_state.get("healthy", True)):
            return EmergencySafetyState(
                active=True,
                reason="mark_price_abnormality",
                halt_new_entries=True,
                cancel_pending_entries=True,
                flatten_open_positions=live_order_count > 0,
                details=mark_price_state,
            )

        suspension_state = await self._suspension_or_delisting_state(active_auto_orders=active_auto_orders)
        if not bool(suspension_state.get("healthy", True)):
            return EmergencySafetyState(
                active=True,
                reason="symbol_suspension_or_delisting",
                halt_new_entries=True,
                cancel_pending_entries=True,
                flatten_open_positions=live_order_count > 0,
                details=suspension_state,
            )

        unsafe_account_details = self._materially_unsafe_account_state_details(
            account_snapshot=account_snapshot,
            active_auto_orders=active_auto_orders,
        )
        if unsafe_account_details is not None:
            return EmergencySafetyState(
                active=True,
                reason="materially_unsafe_account_state",
                halt_new_entries=True,
                cancel_pending_entries=True,
                flatten_open_positions=live_order_count > 0,
                details=unsafe_account_details,
            )

        return EmergencySafetyState(active=False)

    async def _flatten_open_positions(
        self,
        session,
        *,
        reason: str,
        reason_context: dict[str, object],
    ) -> tuple[int, int]:
        flattened = 0
        failures = 0
        for order in await self._active_orders(session, approved_by=self.APPROVED_BY):
            if order.status != OrderStatus.IN_POSITION:
                continue
            try:
                await self.order_manager.close_position(
                    session,
                    order_id=order.id,
                    reason="auto_mode_emergency_flatten",
                    reason_context={
                        "emergency_reason": reason,
                        **reason_context,
                    },
                )
                flattened += 1
            except Exception as exc:
                failures += 1
                logger.warning(
                    "auto_mode.emergency_flatten_failed",
                    order_id=order.id,
                    symbol=order.symbol,
                    error=str(exc),
                )
        return flattened, failures

    async def _apply_emergency_safety_actions(
        self,
        session,
        *,
        scan_cycle_id: int | None,
        active_auto_orders: list[Order],
        account_snapshot,
    ) -> EmergencySafetyState:
        emergency_state = await self._emergency_safety_state(
            session,
            active_auto_orders=active_auto_orders,
            account_snapshot=account_snapshot,
        )
        if not emergency_state.active or not emergency_state.halt_new_entries:
            return emergency_state

        reason_context = {
            "emergency_reason": emergency_state.reason,
            **dict(emergency_state.details or {}),
        }
        cancelled_pending_orders = 0
        if emergency_state.cancel_pending_entries:
            cancelled_pending_orders = await self._cancel_pending_entries(
                session,
                reason="viability_lost",
                reason_context=reason_context,
            )

        flattened_positions = 0
        flatten_failures = 0
        if emergency_state.flatten_open_positions:
            flattened_positions, flatten_failures = await self._flatten_open_positions(
                session,
                reason=str(emergency_state.reason or "emergency"),
                reason_context=reason_context,
            )

        await record_audit(
            session,
            event_type="AUTO_MODE_EMERGENCY_GUARD_TRIGGERED",
            level=AuditLevel.ERROR,
            message="AQRR emergency guard halted entries and executed safety actions",
            scan_cycle_id=scan_cycle_id,
            details={
                "reason": emergency_state.reason,
                "halt_new_entries": emergency_state.halt_new_entries,
                "cancel_pending_entries": emergency_state.cancel_pending_entries,
                "flatten_open_positions": emergency_state.flatten_open_positions,
                "cancelled_pending_orders": cancelled_pending_orders,
                "flattened_positions": flattened_positions,
                "flatten_failures": flatten_failures,
                **dict(emergency_state.details or {}),
            },
        )
        await session.commit()
        return emergency_state

    async def get_status(self, session, *, next_cycle_at: datetime | None) -> AutoModeStatusRead:
        settings_map = await get_settings_map(session)
        config = resolve_strategy_config(settings_map)
        credentials = await self.order_manager.get_credentials(session)
        account_snapshot = await self.order_manager.get_read_account_snapshot(session, credentials)
        shared_slot_budget = await self.order_manager.get_shared_entry_slot_budget(
            session,
            account_snapshot=account_snapshot,
        )
        enabled = self._is_enabled(settings_map)
        paused = self._is_paused(settings_map)
        active_auto_orders = await self._active_orders(session, approved_by=self.APPROVED_BY)
        kill_switch_state = await self._kill_switch_state(
            session,
            settings_map=settings_map,
            account_snapshot=account_snapshot,
            active_auto_orders=active_auto_orders,
        )
        active_risk_usdt = sum((Decimal(order.risk_usdt_at_stop or 0) for order in active_auto_orders), start=Decimal("0"))
        portfolio_risk_budget_usdt = account_snapshot.available_balance * config.max_portfolio_risk_fraction
        remaining_portfolio_risk_usdt = max(portfolio_risk_budget_usdt - active_risk_usdt, Decimal("0"))
        per_trade_risk_budget_usdt = account_snapshot.available_balance * config.risk_per_trade_fraction
        if shared_slot_budget.remaining_entry_slots > 0:
            per_slot_risk_budget_usdt = min(
                per_trade_risk_budget_usdt,
                remaining_portfolio_risk_usdt / Decimal(shared_slot_budget.remaining_entry_slots),
            )
        else:
            per_slot_risk_budget_usdt = Decimal("0")
        latest_cycle = await self._latest_auto_mode_cycle(session)
        return AutoModeStatusRead(
            enabled=enabled,
            paused=paused,
            running=self.running,
            signal_schedule="15m_closed_candle",
            kill_switch_active=kill_switch_state.active,
            kill_switch_reason=kill_switch_state.reason,
            active_order_count=len(active_auto_orders),
            active_risk_usdt=active_risk_usdt,
            portfolio_risk_budget_usdt=portfolio_risk_budget_usdt,
            per_slot_risk_budget_usdt=per_slot_risk_budget_usdt,
            last_cycle_started_at=self.last_cycle_started_at or (latest_cycle.started_at if latest_cycle is not None else None),
            last_cycle_completed_at=self.last_cycle_completed_at or (latest_cycle.completed_at if latest_cycle is not None else None),
            next_cycle_at=next_cycle_at,
        )

    async def _record_skip(self, session, *, message: str, scan_cycle_id: int | None = None, signal_id: int | None = None) -> None:
        await record_audit(
            session,
            event_type="AUTO_MODE_CYCLE_SKIPPED",
            level=AuditLevel.WARNING,
            message=message,
            scan_cycle_id=scan_cycle_id,
            signal_id=signal_id,
        )
        await session.commit()

    async def _entry_distance_filtered_symbols(self, session, *, scan_cycle_id: int) -> set[tuple[str, object]]:
        results = (
            await session.execute(
                select(ScanSymbolResult).where(ScanSymbolResult.scan_cycle_id == scan_cycle_id)
            )
        ).scalars().all()
        filtered_symbols: set[tuple[str, object]] = set()
        for result in results:
            symbol = str(getattr(result, "symbol", "")).upper()
            direction = getattr(result, "direction", None)
            filter_reasons = getattr(result, "filter_reasons", []) or []
            if not symbol or direction is None:
                continue
            if "entry_too_far_from_mark" in filter_reasons:
                filtered_symbols.add((symbol, direction))
        return filtered_symbols

    async def _scan_results_by_symbol(self, session, *, scan_cycle_id: int) -> dict[str, ScanSymbolResult]:
        rows = (
            await session.execute(
                select(ScanSymbolResult).where(ScanSymbolResult.scan_cycle_id == scan_cycle_id)
            )
        ).scalars().all()
        return {
            str(row.symbol).upper(): row
            for row in rows
            if getattr(row, "symbol", None)
        }

    @staticmethod
    def _pending_invalidation_decision(
        *,
        order: Order,
        scan_result: ScanSymbolResult | None,
    ) -> PendingInvalidationDecision:
        if scan_result is None:
            return PendingInvalidationDecision(lifecycle_reason="viability_lost")
        extra_context = dict(getattr(scan_result, "extra_context", {}) or {})
        filter_reasons = set(getattr(scan_result, "filter_reasons", []) or [])
        selection_rejection_reason = str(extra_context.get("selection_rejection_reason") or "")
        market_state = str(extra_context.get("market_state") or "")
        setup_family = str(getattr(order, "setup_family", None) or extra_context.get("setup_family") or "")
        raw_aqrr_reasons = tuple(
            str(reason)
            for reason in (extra_context.get("aqrr_raw_rejection_reasons") or [])
            if isinstance(reason, str) and reason.strip()
        )
        raw_aqrr_reason = str(extra_context.get("aqrr_raw_rejection_reason") or "") or None
        if raw_aqrr_reason is None and raw_aqrr_reasons:
            raw_aqrr_reason = raw_aqrr_reasons[0]
        aqrr_rejection_stage = str(extra_context.get("aqrr_rejection_stage") or "") or None

        if selection_rejection_reason in {"correlation_conflict", "cluster_conflict", "btc_beta_conflict"}:
            return PendingInvalidationDecision(
                lifecycle_reason="correlation_conflict",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        if (
            selection_rejection_reason == "slot_limit_reached"
            or raw_aqrr_reason == "slot_limit_reached"
            or "slot_limit_reached" in raw_aqrr_reasons
        ):
            return PendingInvalidationDecision(
                lifecycle_reason=None,
                cancel_pending=False,
                decision_reason="capacity_rejected",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        if filter_reasons.intersection(
            {
                "spread_above_threshold",
                "spread_relative_above_threshold",
                "spread_unavailable",
                "order_book_unstable",
                "execution_tier_c_rejected",
            }
        ):
            return PendingInvalidationDecision(
                lifecycle_reason="spread_filter_failed",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        if "volatility_shock" in filter_reasons or extra_context.get("volatility_shock") is True:
            return PendingInvalidationDecision(
                lifecycle_reason="volatility_shock",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        if "unstable_no_trade" in filter_reasons or market_state == "UNSTABLE":
            return PendingInvalidationDecision(
                lifecycle_reason="regime_flipped",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        if setup_family == "breakout_retest" and market_state == "BALANCED_RANGE":
            return PendingInvalidationDecision(
                lifecycle_reason="setup_state_changed",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        if filter_reasons.intersection({"invalidation_structure_break", "support_or_resistance_break", "range_structure_break"}):
            return PendingInvalidationDecision(
                lifecycle_reason="structure_invalidated",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        if raw_aqrr_reasons or raw_aqrr_reason is not None or filter_reasons.intersection({"aqrr_hard_filters_failed", "no_aqrr_setup"}):
            return PendingInvalidationDecision(
                lifecycle_reason="viability_lost",
                raw_aqrr_reason=raw_aqrr_reason,
                raw_aqrr_reasons=raw_aqrr_reasons,
                aqrr_rejection_stage=aqrr_rejection_stage,
            )
        return PendingInvalidationDecision(lifecycle_reason="viability_lost")

    @staticmethod
    def _pending_invalidation_reason(*, order: Order, scan_result: ScanSymbolResult | None) -> str | None:
        decision = AutoModeService._pending_invalidation_decision(order=order, scan_result=scan_result)
        return decision.lifecycle_reason or decision.decision_reason

    async def _cancel_pending_entries(
        self,
        session,
        *,
        reason: str,
        reason_context: dict[str, object] | None = None,
    ) -> int:
        cancelled = 0
        for order in await self._active_orders(session, approved_by=self.APPROVED_BY):
            if order.status != OrderStatus.ORDER_PLACED:
                continue
            await self.order_manager.cancel_order(
                session,
                order_id=order.id,
                reason=reason,
                reason_context=reason_context,
            )
            cancelled += 1
        return cancelled

    async def _manage_existing_orders(self, session, *, actionable_signals: list[Signal], scan_cycle_id: int | None = None) -> None:
        active_auto_orders = await self._active_orders(session, approved_by="AUTO_MODE")
        active_local_orders = await self._active_orders(session)
        live_exposure_orders = [
            order
            for order in active_local_orders
            if order.status == OrderStatus.IN_POSITION
        ]
        correlation_threshold = self.DEFAULT_CORRELATION_THRESHOLD
        try:
            correlation_threshold = resolve_strategy_config(await get_settings_map(session)).correlation_reject_threshold
        except Exception as exc:
            logger.warning("auto_mode.pending_correlation_threshold_fallback", error=str(exc))
        now = datetime.now(timezone.utc)
        entry_distance_filtered_symbols = (
            await self._entry_distance_filtered_symbols(session, scan_cycle_id=scan_cycle_id)
            if scan_cycle_id is not None
            else set()
        )
        scan_results_by_symbol = (
            await self._scan_results_by_symbol(session, scan_cycle_id=scan_cycle_id)
            if scan_cycle_id is not None
            else {}
        )
        actionable_by_symbol: dict[str, list[Signal]] = {}
        for signal in actionable_signals:
            actionable_by_symbol.setdefault(signal.symbol.upper(), []).append(signal)
        returns_cache: dict[str, list[float]] = {}

        for order in active_auto_orders:
            symbol = order.symbol.upper()
            symbol_signals = actionable_by_symbol.get(symbol, [])
            pending_expired = (
                self.order_manager.pending_entry_expired(order, now=now)
                if hasattr(self.order_manager, "pending_entry_expired")
                else (order.expires_at is not None and order.expires_at <= now)
            )
            if order.status == OrderStatus.ORDER_PLACED and pending_expired:
                await self.order_manager.sync_order(session, order)
                pending_expired_after_sync = (
                    self.order_manager.pending_entry_expired(order, now=now)
                    if hasattr(self.order_manager, "pending_entry_expired")
                    else (order.expires_at is not None and order.expires_at <= now)
                )
                if order.status == OrderStatus.ORDER_PLACED and pending_expired_after_sync:
                    await self.order_manager.cancel_order(session, order_id=order.id, reason="expired")
                    continue
            if order.status == OrderStatus.ORDER_PLACED:
                try:
                    correlation_conflict = await self._pending_open_correlation_conflict(
                        order=order,
                        live_orders=[item for item in live_exposure_orders if item.id != order.id],
                        correlation_threshold=correlation_threshold,
                        returns_cache=returns_cache,
                    )
                except Exception as exc:
                    logger.warning(
                        "auto_mode.pending_open_correlation_check_failed",
                        order_id=order.id,
                        symbol=order.symbol,
                        error=str(exc),
                    )
                    correlation_conflict = {
                        "reason": "correlation_guard_unavailable",
                        "guard_failure": "exception",
                        "error": str(exc),
                    }
                if correlation_conflict is not None:
                    lifecycle_reason = (
                        "viability_lost"
                        if correlation_conflict.get("reason") == "correlation_guard_unavailable"
                        else "correlation_conflict"
                    )
                    await self.order_manager.cancel_order(
                        session,
                        order_id=order.id,
                        reason=lifecycle_reason,
                        reason_context={
                            "lifecycle_reason": lifecycle_reason,
                            "invalidation_scope": "pending_vs_open",
                            **correlation_conflict,
                        },
                    )
                    continue
            same_direction_signals = [signal for signal in symbol_signals if signal.direction == order.direction]
            if same_direction_signals:
                has_matching_setup_state = any(
                    self._pending_setup_state_matches_signal(order=order, signal=signal)
                    for signal in same_direction_signals
                )
                if has_matching_setup_state:
                    continue
                await self.order_manager.cancel_order(
                    session,
                    order_id=order.id,
                    reason="setup_state_changed",
                    reason_context={
                        "lifecycle_reason": "setup_state_changed",
                        "same_direction_setup_refresh_required": True,
                        "fresh_signal_ids": [signal.id for signal in same_direction_signals if signal.id is not None],
                    },
                )
                continue
            if order.status != OrderStatus.ORDER_PLACED:
                continue
            if any(signal.direction != order.direction for signal in symbol_signals):
                await self.order_manager.cancel_order(session, order_id=order.id, reason="setup_state_changed")
                continue
            if (symbol, order.direction) in entry_distance_filtered_symbols:
                continue
            invalidation_decision = self._pending_invalidation_decision(
                order=order,
                scan_result=scan_results_by_symbol.get(symbol),
            )
            if not invalidation_decision.cancel_pending or invalidation_decision.lifecycle_reason is None:
                await record_audit(
                    session,
                    event_type="AUTO_MODE_PENDING_RETAINED",
                    level=AuditLevel.INFO,
                    message=f"{order.symbol} pending entry retained because rejection was capacity-driven, not thesis invalidation",
                    symbol=order.symbol,
                    order_id=order.id,
                    signal_id=order.signal_id,
                    scan_cycle_id=scan_cycle_id,
                    details={
                        "reason": invalidation_decision.decision_reason or "capacity_rejected",
                        "selection_rejection_reason": str(
                            (
                                getattr(scan_results_by_symbol.get(symbol), "extra_context", {}) or {}
                            ).get("selection_rejection_reason")
                            or ""
                        )
                        or None,
                        "raw_aqrr_reason": invalidation_decision.raw_aqrr_reason,
                        "raw_aqrr_reasons": list(invalidation_decision.raw_aqrr_reasons),
                        "aqrr_rejection_stage": invalidation_decision.aqrr_rejection_stage,
                    },
                )
                continue
            await self.order_manager.cancel_order(
                session,
                order_id=order.id,
                reason=invalidation_decision.lifecycle_reason,
                reason_context={
                    "lifecycle_reason": invalidation_decision.lifecycle_reason,
                    "raw_aqrr_reason": invalidation_decision.raw_aqrr_reason,
                    "raw_aqrr_reasons": list(invalidation_decision.raw_aqrr_reasons),
                    "aqrr_rejection_stage": invalidation_decision.aqrr_rejection_stage,
                },
            )

    async def _live_position_invalidation_reason(self, *, order: Order, config) -> str | None:
        market_health_snapshot = (
            await self.market_health.snapshot(order.symbol)
            if self.market_health is not None
            else None
        )
        execution_degraded = False
        if market_health_snapshot is not None and market_health_snapshot.book_ticker is not None:
            execution_degraded = (
                not market_health_snapshot.book_stable
                or market_health_snapshot.spread_bps is None
                or market_health_snapshot.spread_bps > float(config.max_book_spread_bps)
                or (
                    market_health_snapshot.relative_spread_ready
                    and market_health_snapshot.spread_relative_ratio is not None
                    and market_health_snapshot.spread_relative_ratio > 2.5
                )
            )
        if execution_degraded:
            failure_count = self._live_execution_failures.get(order.id, 0) + 1
            self._live_execution_failures[order.id] = failure_count
            if failure_count >= self.LIVE_SPREAD_DEGRADATION_CLOSE_COUNT:
                return "aqrr_spread_deteriorated"
        else:
            self._live_execution_failures.pop(order.id, None)

        candles_15m = await self._load_closed_candles(
            symbol=order.symbol,
            interval="15m",
            limit=max(260, required_15m_candles_for_volatility_shock(atr_period=config.atr_period_15m)),
        )
        candles_1h = await self._load_closed_candles(symbol=order.symbol, interval="1h", limit=260)
        candles_4h = await self._load_closed_candles(symbol=order.symbol, interval="4h", limit=260)
        assessment = classify_market_state(
            candles_15m=candles_15m,
            candles_1h=candles_1h,
            candles_4h=candles_4h,
            config=config,
        )
        if assessment.market_state == MARKET_STATE_UNSTABLE:
            return "aqrr_unstable_market_state"
        if assessment.direction is not None and assessment.direction != order.direction:
            return "aqrr_regime_flip"
        return None

    async def manage_live_positions(self, session) -> None:
        active_auto_orders = await self._active_orders(session, approved_by=self.APPROVED_BY)
        credentials = await self.order_manager.get_credentials(session)
        account_snapshot = await self.order_manager.get_read_account_snapshot(session, credentials)
        emergency_state = await self._apply_emergency_safety_actions(
            session,
            scan_cycle_id=None,
            active_auto_orders=active_auto_orders,
            account_snapshot=account_snapshot,
        )
        if emergency_state.active and emergency_state.halt_new_entries:
            active_auto_orders = await self._active_orders(session, approved_by=self.APPROVED_BY)
        live_orders = [order for order in active_auto_orders if order.status == OrderStatus.IN_POSITION]
        live_order_ids = {order.id for order in live_orders if order.id is not None}
        for order_id in list(self._live_execution_failures):
            if order_id not in live_order_ids:
                self._live_execution_failures.pop(order_id, None)
        if not live_orders:
            return

        config = resolve_strategy_config(await get_settings_map(session))
        for order in live_orders:
            try:
                invalidation_reason = await self._live_position_invalidation_reason(order=order, config=config)
            except Exception as exc:
                logger.warning("auto_mode.live_position_check_failed", order_id=order.id, symbol=order.symbol, error=str(exc))
                continue
            if invalidation_reason is None:
                continue
            try:
                await self.order_manager.close_position(session, order_id=order.id, reason=invalidation_reason)
                self._live_execution_failures.pop(order.id, None)
            except Exception as exc:
                logger.warning(
                    "auto_mode.live_position_close_failed",
                    order_id=order.id,
                    symbol=order.symbol,
                    reason=invalidation_reason,
                    error=str(exc),
                )

    async def _open_signal_as_pending_order(
        self,
        session,
        *,
        scan_cycle: ScanCycle,
        settings_map: dict[str, str],
        signal: Signal,
        credentials,
        filters_map: dict,
        mark_prices_map: dict,
        leverage_brackets_map: dict,
        active_auto_risk: Decimal,
    ) -> RankedPendingOrder | None:
        if not await self._mode_is_enabled(session):
            return None

        config = resolve_strategy_config(settings_map)
        account_snapshot = await self.order_manager.get_account_snapshot(session, credentials)
        shared_slot_budget = await self.order_manager.get_shared_entry_slot_budget(
            session,
            account_snapshot=account_snapshot,
        )
        per_slot_risk_budget = shared_slot_budget.per_slot_budget
        if per_slot_risk_budget <= 0:
            return None
        portfolio_risk_cap_usdt = account_snapshot.available_balance * config.max_portfolio_risk_fraction
        remaining_portfolio_risk_usdt = max(portfolio_risk_cap_usdt - active_auto_risk, Decimal("0"))
        remaining_entry_slots = max(shared_slot_budget.remaining_entry_slots, 1)
        per_trade_risk_cap_usdt = account_snapshot.available_balance * config.risk_per_trade_fraction
        target_risk_usdt = min(
            per_trade_risk_cap_usdt,
            remaining_portfolio_risk_usdt / Decimal(remaining_entry_slots) if remaining_portfolio_risk_usdt > 0 else Decimal("0"),
        )
        if target_risk_usdt <= 0:
            await self._record_order_skipped(
                session,
                signal=signal,
                message=f"{signal.symbol} skipped because the Auto Mode portfolio risk cap has been fully allocated.",
                details={"reason": "portfolio_risk_cap_reached"},
            )
            return None
        max_entry_distance_pct = resolve_auto_mode_max_entry_distance_fraction(settings_map)

        live_readiness = await self.order_manager.get_live_signal_readiness(
            session,
            signal=signal,
            settings_map=settings_map,
            account_snapshot=account_snapshot,
            filters_map=filters_map,
            leverage_brackets_map=leverage_brackets_map,
            mark_prices_map=mark_prices_map,
            credentials_available=True,
            risk_budget_override_usdt=per_slot_risk_budget,
            target_risk_usdt_override=target_risk_usdt,
            use_stop_distance_position_sizing=True,
        )
        preview = live_readiness.get("order_preview")
        if not live_readiness["can_open_now"]:
            failure_reason = str(live_readiness.get("failure_reason") or f"{signal.symbol} skipped because live readiness checks failed.")
            details: dict[str, str] = {"reason": "live_readiness_failed"}
            if live_readiness.get("failure_reason") is not None:
                details["failure_reason"] = failure_reason
            if isinstance(preview, dict):
                preview_reason = preview.get("reason")
                if preview_reason is not None:
                    details["preview_reason"] = str(preview_reason)
            await self._record_order_skipped(
                session,
                signal=signal,
                message=failure_reason,
                details=details,
            )
            return None
        if preview is None:
            return None

        mark_price = live_readiness.get("mark_price")
        if isinstance(mark_price, Decimal):
            entry_price = Decimal(signal.entry_price)
            distance_pct = self._entry_distance_pct(mark_price=mark_price, entry_price=entry_price)
            if distance_pct is not None and distance_pct > max_entry_distance_pct:
                await self._record_order_skipped(
                    session,
                    signal=signal,
                    message=self._entry_distance_message(
                        symbol=signal.symbol,
                        mark_price=mark_price,
                        entry_price=entry_price,
                        distance_pct=distance_pct,
                        max_distance_pct=max_entry_distance_pct,
                    ),
                    details={
                        "reason": "entry_too_far_from_mark",
                        "mark_price": self._decimal_string(mark_price),
                        "entry_price": self._decimal_string(entry_price),
                        "distance_pct": self._decimal_string(distance_pct),
                        "max_distance_pct": self._decimal_string(max_entry_distance_pct),
                    },
                )
                return None

        if Decimal(str(preview.get("required_initial_margin") or "0")) <= 0:
            return None
        if Decimal(str(preview.get("risk_usdt_at_stop") or "0")) > remaining_portfolio_risk_usdt:
            await self._record_order_skipped(
                session,
                signal=signal,
                message=f"{signal.symbol} skipped because opening it would exceed the Auto Mode portfolio stop-risk cap.",
                details={
                    "reason": "portfolio_risk_cap_exceeded",
                    "remaining_portfolio_risk_usdt": self._decimal_string(remaining_portfolio_risk_usdt),
                    "signal_risk_usdt": str(preview.get("risk_usdt_at_stop") or "0"),
                },
            )
            return None

        try:
            order = await self.order_manager.approve_signal(
                session,
                signal_id=signal.id,
                approved_by=self.APPROVED_BY,
                risk_budget_override_usdt=per_slot_risk_budget,
                target_risk_usdt_override=target_risk_usdt,
                expires_at_override=signal.expires_at,
                use_stop_distance_position_sizing=True,
            )
        except Exception as exc:
            logger.warning("auto_mode.open_order_failed", symbol=signal.symbol, error=str(exc))
            await record_audit(
                session,
                event_type="AUTO_MODE_ORDER_SKIPPED",
                level=AuditLevel.WARNING,
                message=str(exc),
                symbol=signal.symbol,
                scan_cycle_id=scan_cycle.id,
                signal_id=signal.id,
                details=self._signal_reason_context(signal),
            )
            await session.commit()
            return None

        if signal.extra_context.get("drift_requalification") is True:
            await self._delete_drift_symbol(session, symbol=signal.symbol)
            await record_audit(
                session,
                event_type="AUTO_MODE_DRIFT_ORDER_REOPENED",
                level=AuditLevel.INFO,
                message=f"{signal.symbol} reopened after drift requalification",
                symbol=signal.symbol,
                scan_cycle_id=scan_cycle.id,
                signal_id=signal.id,
                details={"reason": "drift_requalification_order_reopened"},
            )

        return RankedPendingOrder(
            order=order,
            final_score=signal.final_score,
            confirmation_score=signal.confirmation_score,
            rank_value=float(signal.rank_value) if signal.rank_value is not None else float(signal.final_score),
        )

    async def _rebalance_pending_orders(self, session, *, scan_cycle: ScanCycle, settings_map: dict[str, str], actionable_signals: list[Signal]) -> RebalanceSummary:
        if not await self._mode_is_enabled(session):
            return RebalanceSummary()

        config = resolve_strategy_config(settings_map)
        max_entry_distance_pct = resolve_auto_mode_max_entry_distance_fraction(settings_map)
        credentials = await self.order_manager.get_credentials(session)
        if credentials is None:
            await self._record_skip(session, message="API credentials are required before Auto Mode can manage live orders.", scan_cycle_id=scan_cycle.id)
            return RebalanceSummary()

        account_snapshot = await self.order_manager.get_account_snapshot(session, credentials)
        shared_slot_budget = await self.order_manager.get_shared_entry_slot_budget(
            session,
            account_snapshot=account_snapshot,
        )
        per_slot_risk_budget = shared_slot_budget.per_slot_budget
        if shared_slot_budget.remaining_entry_slots > 0 and per_slot_risk_budget <= 0:
            await self._record_skip(session, message="Auto Mode has no available shared entry budget right now.", scan_cycle_id=scan_cycle.id)
            return RebalanceSummary()

        try:
            exchange_info = await self.gateway.exchange_info()
            filters_map = self.gateway.parse_symbol_filters(exchange_info)
            mark_prices_map = await self.gateway.mark_prices()
            leverage_brackets_map = await self.gateway.leverage_brackets(credentials)
        except Exception as exc:
            await self._record_skip(
                session,
                message=f"Auto Mode could not load live Binance metadata: {exc}",
                scan_cycle_id=scan_cycle.id,
            )
            return RebalanceSummary()

        active_auto_orders = await self._active_orders(session, approved_by=self.APPROVED_BY)
        emergency_state = await self._apply_emergency_safety_actions(
            session,
            scan_cycle_id=scan_cycle.id,
            active_auto_orders=active_auto_orders,
            account_snapshot=account_snapshot,
        )
        if emergency_state.active and emergency_state.halt_new_entries:
            return RebalanceSummary()
        active_local_orders = await self._active_orders(session)
        locked_orders = [order for order in active_auto_orders if order.status == OrderStatus.IN_POSITION]
        live_exposure_orders = [order for order in active_local_orders if order.status == OrderStatus.IN_POSITION]
        pending_entries = await self._ranked_pending_orders(
            session,
            orders=[order for order in active_auto_orders if order.status == OrderStatus.ORDER_PLACED],
        )
        manual_blocked_symbols = {order.symbol for order in active_local_orders if order.approved_by != "AUTO_MODE"}
        locked_symbols = {order.symbol for order in locked_orders}
        replaceable_pending_order_ids = {order.id for order in active_auto_orders if order.status == OrderStatus.ORDER_PLACED}
        nonreplaceable_global_order_count = len([order for order in active_local_orders if order.id not in replaceable_pending_order_ids])
        shared_slot_cap = int(getattr(self.order_manager, "MAX_SHARED_ENTRY_ORDERS", self.MAX_ACTIVE_ORDERS))
        pending_limit = max(shared_slot_cap - nonreplaceable_global_order_count, 0)
        active_auto_risk = sum((Decimal(order.risk_usdt_at_stop or 0) for order in locked_orders), start=Decimal("0"))
        kill_switch_state = await self._kill_switch_state(
            session,
            settings_map=settings_map,
            account_snapshot=account_snapshot,
            active_auto_orders=active_auto_orders,
        )
        kept_pending_entries: list[RankedPendingOrder] = []
        opened_order_count = 0

        if kill_switch_state.active:
            cancelled_orders = await self._cancel_pending_entries(
                session,
                reason="viability_lost",
                reason_context={"kill_switch_reason": kill_switch_state.reason},
            )
            await record_audit(
                session,
                event_type="AUTO_MODE_KILL_SWITCH_ACTIVE",
                level=AuditLevel.WARNING,
                message="AQRR kill switch suspended new entries",
                scan_cycle_id=scan_cycle.id,
                details={
                    "reason": kill_switch_state.reason,
                    "cancelled_pending_orders": cancelled_orders,
                    "consecutive_stop_losses": kill_switch_state.consecutive_stop_losses,
                    "realized_session_pnl": self._decimal_string(kill_switch_state.realized_session_pnl),
                    "drawdown_pct": self._decimal_string(kill_switch_state.drawdown_fraction * Decimal("100")),
                    "open_risk_plus_loss": self._decimal_string(kill_switch_state.open_risk_plus_loss),
                },
            )
            await session.commit()
            return RebalanceSummary()

        for entry in pending_entries:
            mark_payload = await self._fresh_mark_payload(
                symbol=entry.order.symbol,
                fallback_map=mark_prices_map,
            )
            mark_price = self._decimal_from_payload(None if mark_payload is None else mark_payload.get("markPrice"))
            distance_pct = (
                None
                if mark_price is None
                else self._entry_distance_pct(mark_price=mark_price, entry_price=Decimal(entry.order.entry_price))
            )
            if distance_pct is not None and distance_pct > max_entry_distance_pct:
                await self.order_manager.cancel_order(
                    session,
                    order_id=entry.order.id,
                    reason=self.TOO_FAR_CANCEL_REASON,
                )
                await self._upsert_drift_symbol(
                    session,
                    symbol=entry.order.symbol,
                    planned_entry_price=Decimal(entry.order.entry_price),
                    scan_cycle_id=scan_cycle.id,
                )
                continue
            if entry.order.symbol in manual_blocked_symbols or entry.order.symbol in locked_symbols:
                await self.order_manager.cancel_order(session, order_id=entry.order.id, reason="setup_state_changed")
                continue
            kept_pending_entries.append(entry)
            active_auto_risk += Decimal(entry.order.risk_usdt_at_stop or 0)

        while len(kept_pending_entries) > pending_limit:
            weakest = self._weakest_pending_order(kept_pending_entries)
            if weakest is None:
                break
            await self.order_manager.cancel_order(session, order_id=weakest.order.id, reason="setup_state_changed")
            kept_pending_entries.remove(weakest)
            active_auto_risk -= Decimal(weakest.order.risk_usdt_at_stop or 0)

        if pending_limit <= 0:
            return RebalanceSummary()

        returns_cache: dict[str, list[float]] = {}
        for signal in actionable_signals:
            if not await self._mode_is_enabled(session):
                return RebalanceSummary(opened_order_count=opened_order_count)
            if signal.symbol in manual_blocked_symbols or signal.symbol in locked_symbols:
                continue

            try:
                correlation_conflict = await self._open_position_correlation_conflict(
                    signal=signal,
                    live_orders=live_exposure_orders,
                    correlation_threshold=config.correlation_reject_threshold,
                    returns_cache=returns_cache,
                )
            except Exception as exc:
                logger.warning("auto_mode.correlation_guard_failed", symbol=signal.symbol, error=str(exc))
                await self._record_order_skipped(
                    session,
                    signal=signal,
                    message=(
                        f"{signal.symbol} skipped because correlation safety could not be established "
                        f"due to guard error: {exc}"
                    ),
                    details={
                        "reason": "correlation_guard_unavailable",
                        "guard_failure": "exception",
                        "error": str(exc),
                    },
                )
                continue
            if correlation_conflict is not None:
                if correlation_conflict.get("reason") == "correlation_guard_unavailable":
                    await self._record_order_skipped(
                        session,
                        signal=signal,
                        message=f"{signal.symbol} skipped because correlation safety context is incomplete.",
                        details=correlation_conflict,
                    )
                    continue
                await self._record_order_skipped(
                    session,
                    signal=signal,
                    message=(
                        f"{signal.symbol} skipped due to open-position correlation conflict with "
                        f"{correlation_conflict['conflict_symbol']}."
                    ),
                    details={"reason": "correlation_conflict", **correlation_conflict},
                )
                continue

            current_pending = next((entry for entry in kept_pending_entries if entry.order.symbol == signal.symbol), None)
            replace_target: RankedPendingOrder | None = None
            if current_pending is not None:
                if current_pending.order.direction != signal.direction:
                    continue
                if not self._signal_is_strictly_better(signal, current_pending):
                    continue
                replace_target = current_pending
            elif len(kept_pending_entries) >= pending_limit:
                weakest = self._weakest_pending_order(kept_pending_entries)
                if weakest is None or not self._signal_is_strictly_better(signal, weakest):
                    continue
                replace_target = weakest

            if replace_target is not None:
                await self.order_manager.cancel_order(session, order_id=replace_target.order.id, reason="setup_state_changed")
                kept_pending_entries.remove(replace_target)
                active_auto_risk -= Decimal(replace_target.order.risk_usdt_at_stop or 0)

            opened_entry = await self._open_signal_as_pending_order(
                session,
                scan_cycle=scan_cycle,
                settings_map=settings_map,
                signal=signal,
                credentials=credentials,
                filters_map=filters_map,
                mark_prices_map=mark_prices_map,
                leverage_brackets_map=leverage_brackets_map,
                active_auto_risk=active_auto_risk,
            )
            if opened_entry is None:
                continue

            kept_pending_entries.append(opened_entry)
            active_auto_risk += Decimal(opened_entry.order.risk_usdt_at_stop or 0)
            opened_order_count += 1

        return RebalanceSummary(opened_order_count=opened_order_count)

    async def run_cycle(self, *, reason: str) -> bool:
        current_task = asyncio.current_task()
        if self._has_conflicting_cycle_task(current_task=current_task):
            return False

        final_reason = "cycle_finished"
        async with self._cycle_lock:
            self._active_task = current_task
            if self._queued_task is current_task:
                self._queued_task = None
            self.last_cycle_started_at = datetime.now(timezone.utc)
            await self.broadcast_state(reason="cycle_started")
            try:
                async with self.session_factory() as session:
                    settings_map = await get_settings_map(session)
                    if not self._is_cycle_enabled(settings_map):
                        return False

                    await self._sync_existing_orders(session)
                    ready_drift_symbols = await self._ready_drift_symbols_for_cycle(session)
                    try:
                        scan_cycle = await self.scanner_service.run_scan(
                            session,
                            trigger_type=TriggerType.AUTO_MODE,
                            priority_symbols=ready_drift_symbols,
                        )
                    except RuntimeError as exc:
                        await self._record_skip(session, message=str(exc))
                        return False
                    await self._process_drift_requalification_results(
                        session,
                        scan_cycle_id=scan_cycle.id,
                        ready_symbols=ready_drift_symbols,
                    )

                    actionable_signals = await self._actionable_signals_for_cycle(session, cycle_id=scan_cycle.id)
                    if not await self._mode_is_enabled(session):
                        final_reason = "cycle_cancelled"
                        await self._record_cycle_cancelled(
                            session,
                            reason=reason,
                            cancel_reason="mode_paused" if self._is_paused(await get_settings_map(session)) else "mode_disabled",
                            scan_cycle_id=scan_cycle.id,
                        )
                        return False
                    await self._manage_existing_orders(
                        session,
                        actionable_signals=actionable_signals,
                        scan_cycle_id=scan_cycle.id,
                    )
                    if not await self._mode_is_enabled(session):
                        final_reason = "cycle_cancelled"
                        await self._record_cycle_cancelled(
                            session,
                            reason=reason,
                            cancel_reason="mode_paused" if self._is_paused(await get_settings_map(session)) else "mode_disabled",
                            scan_cycle_id=scan_cycle.id,
                        )
                        return False
                    rebalance_summary = await self._rebalance_pending_orders(
                        session,
                        scan_cycle=scan_cycle,
                        settings_map=settings_map,
                        actionable_signals=actionable_signals,
                    )
                    credentials = await self.order_manager.get_credentials(session)
                    account_snapshot = await self.order_manager.get_account_snapshot(session, credentials)
                    shared_slot_budget = await self.order_manager.get_shared_entry_slot_budget(
                        session,
                        account_snapshot=account_snapshot,
                    )
                    await record_audit(
                        session,
                        event_type="AUTO_MODE_CYCLE_COMPLETE",
                        message="Auto Mode cycle complete",
                        scan_cycle_id=scan_cycle.id,
                        details={
                            "reason": reason,
                            "candidate_count": scan_cycle.candidates_found,
                            "qualified_count": len(actionable_signals),
                            "active_slot_count": shared_slot_budget.active_entry_order_count,
                            "remaining_slot_count": shared_slot_budget.remaining_entry_slots,
                            "opened_order_count": rebalance_summary.opened_order_count,
                            "skipped_because_no_qualified_signals": len(actionable_signals) == 0,
                        },
                    )
                    await session.commit()
                    return True
            except asyncio.CancelledError:
                final_reason = "cycle_cancelled"
                logger.info("auto_mode.run_cycle_cancelled", reason=reason)
                async with self.session_factory() as session:
                    await self._record_cycle_cancelled(
                        session,
                        reason=reason,
                        cancel_reason="task_cancelled",
                    )
                return False
            except Exception as exc:
                logger.error("auto_mode.run_cycle_failed", error=str(exc))
                async with self.session_factory() as session:
                    await record_audit(
                        session,
                        event_type="AUTO_MODE_CYCLE_FAILED",
                        level=AuditLevel.ERROR,
                        message=str(exc),
                        details={"reason": reason},
                    )
                    await session.commit()
                return False
            finally:
                self.last_cycle_completed_at = datetime.now(timezone.utc)
                await self.broadcast_state(reason=final_reason)
                if self._active_task is current_task:
                    self._active_task = None
                if self._queued_task is current_task or (self._queued_task is not None and self._queued_task.done()):
                    self._queued_task = None
