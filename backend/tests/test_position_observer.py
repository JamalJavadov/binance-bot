from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.models.observed_position  # noqa: F401
import app.models.order  # noqa: F401
import app.models.position_pnl_snapshot  # noqa: F401
import app.models.scan_cycle  # noqa: F401
import app.models.scan_symbol_result  # noqa: F401
import app.models.signal  # noqa: F401
from app.models.audit_log import AuditLog
from app.models.enums import OrderStatus, SignalDirection
from app.models.observed_position import ObservedPosition
from app.models.order import Order
from app.models.position_pnl_snapshot import PositionPnlSnapshot
from app.services.position_observer import PositionObserver


class FakeSession:
    def __init__(self) -> None:
        self.orders: dict[int, Order] = {}
        self.observed_positions: dict[tuple[str, str], ObservedPosition] = {}
        self.snapshots: list[PositionPnlSnapshot] = []
        self.added: list[object] = []
        self._ids: dict[type, int] = {}

    def add(self, obj: object) -> None:
        obj_type = type(obj)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            next_id = self._ids.get(obj_type, 0) + 1
            self._ids[obj_type] = next_id
            setattr(obj, "id", next_id)
        self.added.append(obj)
        if isinstance(obj, ObservedPosition):
            self.observed_positions[(obj.symbol.upper(), obj.position_side.upper())] = obj
        elif isinstance(obj, PositionPnlSnapshot):
            self.snapshots.append(obj)

    async def flush(self) -> None:
        return None

    async def get(self, model, obj_id: int):
        if model is Order:
            return self.orders.get(obj_id)
        return None


class StubGateway:
    def __init__(self, positions_payload: list[dict]) -> None:
        self.positions_payload = positions_payload

    async def positions(self, _credentials) -> list[dict]:
        return list(self.positions_payload)


class StubOrderManager:
    def __init__(self) -> None:
        self.sync_calls: list[int] = []
        self.sync_updates: dict[int, OrderStatus] = {}

    async def get_credentials(self, _session):
        return SimpleNamespace(api_key="key", private_key_pem="private")

    async def sync_order(self, _session, order: Order):
        self.sync_calls.append(order.id)
        next_status = self.sync_updates.get(order.id)
        if next_status is not None:
            order.status = next_status
        return order

    async def recover_closed_order(self, session, order: Order):
        return await self.sync_order(session, order)


class ObserverUnderTest(PositionObserver):
    async def _positions_by_key(self, session) -> dict[tuple[str, str], ObservedPosition]:
        return dict(session.observed_positions)

    async def _managed_orders_by_symbol_direction(self, session) -> dict[tuple[str, SignalDirection], Order]:
        status_priority = {
            OrderStatus.IN_POSITION: 3,
            OrderStatus.ORDER_PLACED: 2,
            OrderStatus.SUBMITTING: 1,
        }
        mapping: dict[tuple[str, SignalDirection], Order] = {}
        for order in session.orders.values():
            if order.status not in {OrderStatus.SUBMITTING, OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION}:
                continue
            key = (order.symbol.upper(), order.direction)
            existing = mapping.get(key)
            if existing is None or status_priority.get(order.status, 0) > status_priority.get(existing.status, 0):
                mapping[key] = order
        return mapping

    async def list_open_positions(self, session) -> list[ObservedPosition]:
        return [row for row in session.observed_positions.values() if row.closed_at is None]


def make_order(*, order_id: int, symbol: str, direction: SignalDirection, status: OrderStatus) -> Order:
    return Order(
        id=order_id,
        signal_id=order_id,
        symbol=symbol,
        direction=direction,
        leverage=5,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        quantity=Decimal("1"),
        position_margin=Decimal("20"),
        notional_value=Decimal("100"),
        rr_ratio=Decimal("3"),
        risk_budget_usdt=Decimal("20"),
        risk_usdt_at_stop=Decimal("5"),
        risk_pct_of_wallet=Decimal("5"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=status,
        expires_at=datetime.now(timezone.utc),
        approved_by="AUTO_MODE",
    )


@pytest.mark.asyncio
async def test_sync_positions_persists_external_positions_and_summary() -> None:
    session = FakeSession()
    order_manager = StubOrderManager()
    observer = ObserverUnderTest(
        StubGateway(
            [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.75000000",
                    "positionSide": "BOTH",
                    "entryPrice": "100.0",
                    "markPrice": "110.0",
                    "leverage": "7",
                    "unRealizedProfit": "7.5",
                }
            ]
        ),
        order_manager,
    )

    await observer.sync_positions(session)
    rows = await observer.position_rows(session)
    summary = await observer.portfolio_summary(session)

    assert len(rows) == 1
    assert rows[0].symbol == "BTCUSDT"
    assert rows[0].source_kind == "EXTERNAL"
    assert rows[0].linked_order_id is None
    assert rows[0].mark_price == 110.0
    assert len(session.snapshots) == 1
    assert summary.open_position_count == 1
    assert summary.winning_position_count == 1
    assert summary.losing_position_count == 0
    assert summary.total_unrealized_pnl == 7.5
    assert observer.last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_positions_links_live_remote_exposure_and_promotes_order_before_in_position_status() -> None:
    session = FakeSession()
    order_manager = StubOrderManager()
    order_manager.sync_updates[1] = OrderStatus.IN_POSITION
    order = make_order(order_id=1, symbol="ETHUSDT", direction=SignalDirection.LONG, status=OrderStatus.ORDER_PLACED)
    session.orders[order.id] = order
    observer = ObserverUnderTest(
        StubGateway(
            [
                {
                    "symbol": "ETHUSDT",
                    "positionAmt": "0.50000000",
                    "positionSide": "BOTH",
                    "entryPrice": "100.0",
                    "markPrice": "103.5",
                    "leverage": "5",
                    "unRealizedProfit": "1.75",
                }
            ]
        ),
        order_manager,
    )

    await observer.sync_positions(session)

    observed = session.observed_positions[("ETHUSDT", "BOTH")]
    assert order_manager.sync_calls == [order.id]
    assert order.status == OrderStatus.IN_POSITION
    assert observed.source_kind == "APP_LINKED"
    assert observed.linked_order_id == order.id
    assert observed.closed_at is None


@pytest.mark.asyncio
async def test_sync_positions_marks_missing_linked_positions_closed_externally() -> None:
    session = FakeSession()
    order_manager = StubOrderManager()
    order = make_order(order_id=1, symbol="ETHUSDT", direction=SignalDirection.LONG, status=OrderStatus.IN_POSITION)
    session.orders[order.id] = order
    observed = ObservedPosition(
        id=1,
        symbol="ETHUSDT",
        position_side="BOTH",
        direction=SignalDirection.LONG,
        source_kind="APP_LINKED",
        linked_order_id=order.id,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("104"),
        leverage=5,
        unrealized_pnl=Decimal("4"),
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        closed_at=None,
    )
    session.observed_positions[(observed.symbol.upper(), observed.position_side.upper())] = observed
    observer = ObserverUnderTest(StubGateway([]), order_manager)

    await observer.sync_positions(session)

    assert order_manager.sync_calls == [order.id]
    assert order.status == OrderStatus.CLOSED_EXTERNALLY
    assert order.close_type == "EXTERNAL"
    assert order.closed_at is not None
    assert observed.closed_at is not None
    assert any(isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_EXTERNALLY" for item in session.added)


@pytest.mark.asyncio
async def test_sync_positions_keeps_natural_exit_status_when_sync_proves_close() -> None:
    session = FakeSession()
    order_manager = StubOrderManager()
    order_manager.sync_updates[1] = OrderStatus.CLOSED_WIN
    order = make_order(order_id=1, symbol="ETHUSDT", direction=SignalDirection.LONG, status=OrderStatus.IN_POSITION)
    session.orders[order.id] = order
    observed = ObservedPosition(
        id=1,
        symbol="ETHUSDT",
        position_side="BOTH",
        direction=SignalDirection.LONG,
        source_kind="APP_LINKED",
        linked_order_id=order.id,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("104"),
        leverage=5,
        unrealized_pnl=Decimal("4"),
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
        closed_at=None,
    )
    session.observed_positions[(observed.symbol.upper(), observed.position_side.upper())] = observed
    observer = ObserverUnderTest(StubGateway([]), order_manager)

    await observer.sync_positions(session)

    assert order.status == OrderStatus.CLOSED_WIN
    assert order.close_type is None
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_EXTERNALLY" for item in session.added)
