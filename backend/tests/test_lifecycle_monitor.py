import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.services.lifecycle_monitor as lifecycle_monitor_module
from app.models.enums import OrderStatus, SignalDirection
from app.services.lifecycle_monitor import LifecycleMonitor


class FakeScalarResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def all(self):
        return self.rows


class FakeExecuteResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return FakeScalarResult(self.rows)


class FakeSession:
    def __init__(self, orders: list[object], *, on_commit=None) -> None:
        self.orders = orders
        self.on_commit = on_commit
        self.committed = False

    async def execute(self, _query):
        return FakeExecuteResult(self.orders)

    async def commit(self) -> None:
        self.committed = True
        if self.on_commit is not None:
            self.on_commit()


class DummyAsyncContext:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class StubOrderManager:
    def __init__(self) -> None:
        self.reconcile_calls: list[int] = []
        self.sync_calls: list[int] = []
        self.cancel_calls: list[tuple[int, str]] = []
        self.sibling_calls: list[int] = []

    async def reconcile_managed_orders(self, session, *, approved_by: str | None = None):
        self.reconcile_calls.append(0)

    async def sync_order(self, _session, order):
        self.sync_calls.append(order.id)
        order.status = OrderStatus.IN_POSITION
        return order

    async def cancel_order(self, _session, *, order_id: int, reason: str):
        self.cancel_calls.append((order_id, reason))

    async def cancel_sibling_pending_orders(self, _session, order):
        self.sibling_calls.append(order.id)


class StubPositionObserver:
    def __init__(self) -> None:
        self.sync_calls = 0

    async def sync_positions(self, _session):
        self.sync_calls += 1


class EventAwareOrderManager(StubOrderManager):
    def __init__(self) -> None:
        super().__init__()
        self.exchange_events: list[str] = []

    def handle_user_stream_event(self, payload: dict):
        event_type = str(payload.get("e") or "").upper()
        handled = event_type in {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"}
        if handled:
            self.exchange_events.append(event_type)
        return {"handled": handled, "event_type": event_type}

    def consume_user_stream_supervision_events(self):
        return {
            "count": len(self.exchange_events),
        }


class StubAutoModeService:
    def __init__(self) -> None:
        self.manage_calls = 0

    async def manage_live_positions(self, _session):
        self.manage_calls += 1


def make_order(*, order_id: int, expires_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        signal_id=order_id,
        symbol="BCHUSDT",
        direction=SignalDirection.LONG,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.ORDER_PLACED,
        expires_at=expires_at,
        approved_by="AUTO_MODE",
    )


async def wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition was not met before timeout")


@pytest.mark.asyncio
async def test_lifecycle_monitor_syncs_before_stale_expiry_cancellation(monkeypatch) -> None:
    order = make_order(
        order_id=1,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    order_manager = StubOrderManager()
    position_observer = StubPositionObserver()
    monitor = LifecycleMonitor(order_manager, position_observer, poll_seconds=0)
    session = FakeSession([order], on_commit=monitor._stop.set)

    monkeypatch.setattr(
        lifecycle_monitor_module,
        "AsyncSessionLocal",
        lambda: DummyAsyncContext(session),
    )

    await asyncio.wait_for(monitor.run(), timeout=1)

    assert order_manager.reconcile_calls == [0]
    assert order_manager.sync_calls == [1]
    assert order_manager.cancel_calls == []
    assert order_manager.sibling_calls == [1]
    assert order.status == OrderStatus.IN_POSITION
    assert position_observer.sync_calls == 1
    assert session.committed is True


@pytest.mark.asyncio
async def test_lifecycle_monitor_attempts_order_manager_recovery_before_expiry_cancel(monkeypatch) -> None:
    call_order: list[str] = []

    class RecoveryTrackingOrderManager(StubOrderManager):
        async def reconcile_managed_orders(self, session, *, approved_by: str | None = None):
            call_order.append("reconcile")

        async def sync_order(self, _session, order):
            call_order.append("sync")
            return order

        async def cancel_order(self, _session, *, order_id: int, reason: str):
            call_order.append("cancel")
            self.cancel_calls.append((order_id, reason))

    order = make_order(
        order_id=1,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    order_manager = RecoveryTrackingOrderManager()
    position_observer = StubPositionObserver()
    monitor = LifecycleMonitor(order_manager, position_observer, poll_seconds=0)
    session = FakeSession([order], on_commit=monitor._stop.set)

    monkeypatch.setattr(
        lifecycle_monitor_module,
        "AsyncSessionLocal",
        lambda: DummyAsyncContext(session),
    )

    await asyncio.wait_for(monitor.run(), timeout=1)

    assert call_order == ["reconcile", "sync", "cancel"]
    assert order_manager.cancel_calls == [(1, "expired")]
    assert position_observer.sync_calls == 1


@pytest.mark.asyncio
async def test_lifecycle_monitor_wakes_immediately_on_order_trade_update_event(monkeypatch) -> None:
    order = make_order(
        order_id=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    order_manager = EventAwareOrderManager()
    position_observer = StubPositionObserver()
    monitor = LifecycleMonitor(order_manager, position_observer, poll_seconds=3600)
    session = FakeSession([order])

    monkeypatch.setattr(
        lifecycle_monitor_module,
        "AsyncSessionLocal",
        lambda: DummyAsyncContext(session),
    )

    await monitor.start()
    try:
        await wait_until(lambda: len(order_manager.sync_calls) >= 1)
        await monitor.notify_exchange_event(
            {
                "e": "ORDER_TRADE_UPDATE",
                "o": {"s": "BCHUSDT", "X": "PARTIALLY_FILLED", "x": "TRADE"},
            }
        )
        await wait_until(lambda: len(order_manager.sync_calls) >= 2)
    finally:
        await monitor.stop()

    assert "ORDER_TRADE_UPDATE" in order_manager.exchange_events
    assert position_observer.sync_calls >= 2


@pytest.mark.asyncio
async def test_lifecycle_monitor_wakes_immediately_on_account_update_event(monkeypatch) -> None:
    order_manager = EventAwareOrderManager()
    position_observer = StubPositionObserver()
    auto_mode_service = StubAutoModeService()
    monitor = LifecycleMonitor(
        order_manager,
        position_observer,
        auto_mode_service=auto_mode_service,
        poll_seconds=3600,
    )
    session = FakeSession([])

    monkeypatch.setattr(
        lifecycle_monitor_module,
        "AsyncSessionLocal",
        lambda: DummyAsyncContext(session),
    )

    await monitor.start()
    try:
        await wait_until(lambda: auto_mode_service.manage_calls >= 1)
        await monitor.notify_exchange_event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {"m": "ORDER"},
            }
        )
        await wait_until(lambda: auto_mode_service.manage_calls >= 2)
    finally:
        await monitor.stop()

    assert "ACCOUNT_UPDATE" in order_manager.exchange_events


@pytest.mark.asyncio
async def test_lifecycle_monitor_prioritizes_orders_touched_by_order_trade_update(monkeypatch) -> None:
    class PrioritizedOrderManager(EventAwareOrderManager):
        def consume_user_stream_supervision_events(self):
            return {
                "prioritized_symbols": ["BCHUSDT"],
            }

    order_manager = PrioritizedOrderManager()
    position_observer = StubPositionObserver()
    monitor = LifecycleMonitor(order_manager, position_observer, poll_seconds=3600)
    orders = [
        make_order(order_id=1, expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)),
        make_order(order_id=2, expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)),
    ]
    orders[0].symbol = "AAAUSDT"
    orders[1].symbol = "BCHUSDT"
    session = FakeSession(orders)

    monkeypatch.setattr(
        lifecycle_monitor_module,
        "AsyncSessionLocal",
        lambda: DummyAsyncContext(session),
    )

    await monitor.start()
    try:
        await wait_until(lambda: len(order_manager.sync_calls) >= 2)
        order_manager.sync_calls.clear()
        await monitor.notify_exchange_event(
            {
                "e": "ORDER_TRADE_UPDATE",
                "o": {"s": "BCHUSDT", "X": "PARTIALLY_FILLED", "x": "TRADE"},
            }
        )
        await wait_until(lambda: len(order_manager.sync_calls) >= 2)
    finally:
        await monitor.stop()

    assert order_manager.sync_calls[:2] == [2, 1]


@pytest.mark.asyncio
async def test_lifecycle_monitor_prioritizes_position_refresh_on_account_update(monkeypatch) -> None:
    call_order: list[str] = []

    class AccountAwareOrderManager(EventAwareOrderManager):
        async def sync_order(self, _session, order):
            call_order.append(f"sync:{order.id}")
            return await super().sync_order(_session, order)

        def consume_user_stream_supervision_events(self):
            return {
                "account_refresh_pending": True,
            }

    class OrderedPositionObserver(StubPositionObserver):
        async def sync_positions(self, _session):
            call_order.append("positions")
            await super().sync_positions(_session)

    order_manager = AccountAwareOrderManager()
    position_observer = OrderedPositionObserver()
    monitor = LifecycleMonitor(order_manager, position_observer, poll_seconds=3600)
    session = FakeSession([make_order(order_id=1, expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))])

    monkeypatch.setattr(
        lifecycle_monitor_module,
        "AsyncSessionLocal",
        lambda: DummyAsyncContext(session),
    )

    await monitor.start()
    try:
        await wait_until(lambda: len(call_order) >= 2)
        call_order.clear()
        await monitor.notify_exchange_event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {"m": "ORDER"},
            }
        )
        await wait_until(lambda: len(call_order) >= 2)
    finally:
        await monitor.stop()

    assert call_order[:2] == ["positions", "sync:1"]
