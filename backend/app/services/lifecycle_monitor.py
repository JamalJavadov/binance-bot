import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal
from app.models.enums import OrderStatus
from app.models.order import Order

logger = get_logger(__name__)


class LifecycleMonitor:
    def __init__(self, order_manager, position_observer, auto_mode_service=None, poll_seconds: int = 60) -> None:
        self.order_manager = order_manager
        self.position_observer = position_observer
        self.auto_mode_service = auto_mode_service
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake_up = asyncio.Event()
        self._event_lock = asyncio.Lock()
        self._pending_exchange_events: list[dict[str, Any]] = []

    async def notify_exchange_event(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        event_type = str(payload.get("e") or "").upper()
        handled = event_type in {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"}
        normalized_event: dict[str, Any] = {"handled": handled, "event_type": event_type}
        event_handler = getattr(self.order_manager, "handle_user_stream_event", None)
        if callable(event_handler):
            try:
                candidate = event_handler(payload)
                if isinstance(candidate, dict):
                    normalized_event = candidate
                    handled = bool(candidate.get("handled", handled))
            except Exception as exc:
                logger.warning("lifecycle_monitor.exchange_event_rejected", event_type=event_type, error=str(exc))
                return
        if not handled:
            return
        async with self._event_lock:
            self._pending_exchange_events.append(normalized_event)
        self._wake_up.set()

    async def _drain_exchange_events(self) -> list[dict[str, Any]]:
        async with self._event_lock:
            events = list(self._pending_exchange_events)
            self._pending_exchange_events.clear()
            if not self._pending_exchange_events:
                self._wake_up.clear()
        return events

    async def _wait_for_next_cycle(self) -> None:
        stop_wait = asyncio.create_task(self._stop.wait())
        wake_wait = asyncio.create_task(self._wake_up.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_wait, wake_wait},
                timeout=self.poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if wake_wait in done:
                self._wake_up.clear()
        finally:
            for task in (stop_wait, wake_wait):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def run(self) -> None:
        while not self._stop.is_set():
            exchange_events = await self._drain_exchange_events()
            supervision_summary: dict[str, Any] = {}
            if exchange_events and hasattr(self.order_manager, "consume_user_stream_supervision_events"):
                with contextlib.suppress(Exception):
                    supervision_summary = self.order_manager.consume_user_stream_supervision_events()
            try:
                async with AsyncSessionLocal() as session:
                    await self.order_manager.reconcile_managed_orders(session)
                    prioritize_positions = bool(supervision_summary.get("account_refresh_pending"))
                    prioritized_symbols = {
                        str(symbol).upper()
                        for symbol in supervision_summary.get("prioritized_symbols", [])
                        if isinstance(symbol, str) and symbol.strip()
                    }
                    if prioritize_positions:
                        await self.position_observer.sync_positions(session)
                    orders = (
                        await session.execute(
                            select(Order).where(
                                Order.status.in_([OrderStatus.SUBMITTING, OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION])
                            )
                        )
                    ).scalars().all()
                    if prioritized_symbols:
                        orders.sort(key=lambda order: (str(getattr(order, "symbol", "")).upper() not in prioritized_symbols, order.id))
                    for order in orders:
                        await self.order_manager.sync_order(session, order)
                        pending_expired = (
                            self.order_manager.pending_entry_expired(order, now=datetime.now(timezone.utc))
                            if hasattr(self.order_manager, "pending_entry_expired")
                            else datetime.now(timezone.utc) >= order.expires_at
                        )
                        if order.status == OrderStatus.ORDER_PLACED and pending_expired:
                            await self.order_manager.cancel_order(session, order_id=order.id, reason="expired")
                            continue
                        if order.status == OrderStatus.IN_POSITION:
                            await self.order_manager.cancel_sibling_pending_orders(session, order)
                    if not prioritize_positions:
                        await self.position_observer.sync_positions(session)
                    if self.auto_mode_service is not None:
                        await self.auto_mode_service.manage_live_positions(session)
                    await session.commit()
            except Exception as exc:
                logger.error("lifecycle_monitor.failed", error=str(exc))
            if self._stop.is_set():
                break
            await self._wait_for_next_cycle()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        self._wake_up.set()
        if self._task is not None:
            await self._task
