import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.audit_log import AuditLog
import app.models.order  # noqa: F401
import app.models.scan_cycle  # noqa: F401
import app.models.scan_symbol_result  # noqa: F401
import app.routers.auto_mode as auto_mode_router
import app.services.auto_mode as auto_mode_module
import app.services.scheduler as scheduler_module
from app.models.enums import OrderStatus, ScanStatus, SignalDirection, SignalStatus, TriggerType
from app.models.order import Order
from app.models.scan_cycle import ScanCycle
from app.models.signal import Signal
from app.routers.auto_mode import get_auto_mode_status, update_auto_mode
from app.schemas.auto_mode import AutoModeStatusRead, AutoModeUpdateRequest
from app.services.auto_mode import AutoModeService, RankedPendingOrder
from app.services.binance_gateway import SymbolFilters
from app.services.order_manager import AccountSnapshot, OrderManager, SharedEntrySlotBudget
from app.services.scheduler import SchedulerService
from app.services.strategy.config import resolve_strategy_config
from app.services.strategy.indicators import required_15m_candles_for_volatility_shock
from app.services.ws_manager import WebSocketManager


AUTO_MODE_SETTINGS = {
    "risk_per_trade_pct": "2.0",
    "max_portfolio_risk_pct": "6.0",
    "max_leverage": "10",
    "deployable_equity_pct": "90",
    "max_book_spread_bps": "12",
    "min_24h_quote_volume_usdt": "25000000",
    "kill_switch_consecutive_stop_losses": "2",
    "kill_switch_daily_drawdown_pct": "4.0",
    "auto_mode_max_entry_drift_pct": "5.0",
}

AUTO_MODE_RUNTIME_SETTINGS = {
    **AUTO_MODE_SETTINGS,
}


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.order_map: dict[int, Order] = {}
        self.signal_map: dict[int, Signal] = {}
        self.audits: list[str] = []
        self.scan_result_rows: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def execute(self, statement) -> object:
        if "scan_symbol_results" in str(statement):
            return FakeExecuteResult(self.scan_result_rows)
        return FakeExecuteResult([])


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

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class DummyNotifier:
    async def send(self, **_payload):
        return None


def make_signal(*, signal_id: int, symbol: str, direction: SignalDirection, cycle_id: int) -> Signal:
    now = datetime.now(timezone.utc)
    return Signal(
        id=signal_id,
        scan_cycle_id=cycle_id,
        symbol=symbol,
        direction=direction,
        timeframe="15m",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        rr_ratio=Decimal("3.0"),
        confirmation_score=70,
        final_score=90,
        rank_value=Decimal("90"),
        score_breakdown={"trend": 70},
        reason_text="qualified",
        swing_origin=Decimal("90"),
        swing_terminus=Decimal("110"),
        fib_0786_level=None,
        current_price_at_signal=Decimal("100"),
        expires_at=now + timedelta(minutes=45),
        status=SignalStatus.QUALIFIED,
        extra_context={},
        created_at=now,
        updated_at=now,
    )


def make_order(
    *,
    order_id: int,
    symbol: str,
    status: OrderStatus,
    approved_by: str = "AUTO_MODE",
    direction: SignalDirection = SignalDirection.LONG,
    risk_usdt_at_stop: Decimal = Decimal("1"),
) -> Order:
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
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("26.67"),
        risk_usdt_at_stop=risk_usdt_at_stop,
        risk_pct_of_wallet=Decimal("20"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by=approved_by,
    )


class StubOrderManager:
    BALANCE_RESERVE_FRACTION = OrderManager.BALANCE_RESERVE_FRACTION
    MAX_SHARED_ENTRY_ORDERS = OrderManager.MAX_SHARED_ENTRY_ORDERS

    def __init__(self) -> None:
        self.cancel_calls: list[tuple[int, str]] = []
        self.close_calls: list[tuple[int, str]] = []
        self.cancel_reason_contexts: list[dict[str, object] | None] = []
        self.close_reason_contexts: list[dict[str, object] | None] = []
        self.approve_calls: list[dict] = []
        self.sync_calls: list[int] = []
        self.sync_updates: dict[int, OrderStatus] = {}
        self.readiness_by_signal_id: dict[int, dict] = {}
        self.available_balance = Decimal("100")
        self.shared_slot_budget_override: SharedEntrySlotBudget | None = None
        self.user_stream_health_payload: dict[str, object] = {
            "healthy": True,
            "required": False,
            "mode": "polling_fallback",
        }
        self.order_update_integrity_payload: dict[str, object] = {
            "healthy": True,
            "failure_count": 0,
            "threshold": 3,
            "lookback_minutes": 15,
        }

    async def get_credentials(self, _session):
        return SimpleNamespace(api_key="key", private_key_pem="private")

    async def get_account_snapshot(self, _session, _credentials):
        return AccountSnapshot.from_available_balance(self.available_balance, reserve_fraction=self.BALANCE_RESERVE_FRACTION)

    async def get_read_account_snapshot(self, session, credentials):
        return await self.get_account_snapshot(session, credentials)

    async def user_data_stream_health(self, _session, _credentials):
        return dict(self.user_stream_health_payload)

    async def order_update_integrity_state(self, _session):
        return dict(self.order_update_integrity_payload)

    async def get_shared_entry_slot_budget(self, session, *, account_snapshot: AccountSnapshot | None = None):
        if self.shared_slot_budget_override is not None:
            return self.shared_slot_budget_override
        snapshot = account_snapshot or await self.get_account_snapshot(session, None)
        active_orders = [
            order
            for order in session.order_map.values()
            if order.status in {OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION}
        ]
        remaining_entry_slots = max(self.MAX_SHARED_ENTRY_ORDERS - len(active_orders), 0)
        deployable_equity = snapshot.wallet_balance * (Decimal("1") - self.BALANCE_RESERVE_FRACTION)
        remaining_deployable_equity = min(snapshot.available_balance, deployable_equity)
        per_slot_budget = (
            remaining_deployable_equity / Decimal(remaining_entry_slots)
            if remaining_entry_slots > 0
            else Decimal("0")
        )
        return SharedEntrySlotBudget(
            slot_cap=self.MAX_SHARED_ENTRY_ORDERS,
            active_entry_order_count=len(active_orders),
            remaining_entry_slots=remaining_entry_slots,
            active_symbols=frozenset(order.symbol.upper() for order in active_orders),
            deployable_equity=deployable_equity,
            committed_initial_margin=Decimal("0"),
            remaining_deployable_equity=remaining_deployable_equity,
            portfolio_budget=remaining_deployable_equity,
            per_slot_budget=per_slot_budget,
        )

    async def cancel_order(self, session, *, order_id: int, reason: str, reason_context: dict[str, object] | None = None):
        self.cancel_calls.append((order_id, reason))
        self.cancel_reason_contexts.append(reason_context)
        order = session.order_map.get(order_id)
        if order is not None:
            order.status = OrderStatus.CANCELLED_BY_BOT

    async def close_position(self, session, *, order_id: int, reason: str, reason_context: dict[str, object] | None = None):
        self.close_calls.append((order_id, reason))
        self.close_reason_contexts.append(reason_context)
        order = session.order_map.get(order_id)
        if order is not None:
            order.status = OrderStatus.CLOSED_BY_BOT

    async def sync_order(self, _session, order: Order):
        self.sync_calls.append(order.id)
        updated_status = self.sync_updates.get(order.id)
        if updated_status is not None:
            order.status = updated_status
        return order

    async def reconcile_managed_orders(self, _session, *, approved_by: str | None = None):
        return None

    async def get_live_signal_readiness(self, _session, *, signal: Signal, **_kwargs):
        override = self.readiness_by_signal_id.get(signal.id)
        if override is not None:
            return override
        return {
            "mark_price": Decimal("100"),
            "order_preview": {
                "status": "affordable",
                "can_place": True,
                "auto_resized": False,
                "requested_quantity": "0.1",
                "final_quantity": "0.1",
                "max_affordable_quantity": "0.1",
                "mark_price_used": "100",
                "entry_notional": "10",
                "required_initial_margin": "2",
                "estimated_entry_fee": "0.01",
                "available_balance": "100",
                "reserve_balance": "10",
                "usable_balance": "90",
                "risk_budget_usdt": "2",
                "risk_usdt_at_stop": "1",
                "recommended_leverage": 5,
                "reason": None,
            },
            "can_open_now": True,
            "failure_reason": None,
        }

    async def approve_signal(
        self,
        session,
        *,
        signal_id: int,
        validity_hours: int | None = None,
        approved_by: str,
        risk_budget_override_usdt: Decimal,
        expires_at_override: datetime | None = None,
        target_risk_usdt_override: Decimal | None = None,
        **_kwargs,
    ):
        signal = session.signal_map[signal_id]
        self.approve_calls.append(
            {
                "signal_id": signal_id,
                "validity_hours": validity_hours,
                "approved_by": approved_by,
                "risk_budget_override_usdt": risk_budget_override_usdt,
                "expires_at_override": expires_at_override,
                "target_risk_usdt_override": target_risk_usdt_override,
            }
        )
        order = make_order(
            order_id=signal_id,
            symbol=signal.symbol,
            status=OrderStatus.ORDER_PLACED,
            direction=signal.direction,
        )
        session.order_map[order.id] = order
        return order


class RuntimeUserStreamOrderManager(StubOrderManager):
    USER_STREAM_STALE_MULTIPLIER = 1
    USER_STREAM_STALE_MIN_SECONDS = 1
    USER_STREAM_STALE_CONSECUTIVE_FAILURES = 1

    def __init__(self) -> None:
        super().__init__()
        self._lifecycle_poll_seconds = 1
        self._user_stream_health_started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        self._last_user_stream_order_update_at = None
        self._last_user_stream_account_update_at = None
        self._last_user_stream_event_at = None
        self._user_stream_stale_check_streak = 0
        self._user_stream_primary_available = True
        self._user_stream_primary_reason = "runtime_test"
        self._pending_order_trade_update_events = 0
        self._pending_account_update_events = 0
        self._pending_user_stream_symbols = set()
        self._pending_user_stream_position_symbols = set()
        self._pending_user_stream_trade_execution = False
        self._pending_user_stream_account_refresh = False
        self._last_user_stream_account_snapshot = None

    async def active_entry_orders(self, session):
        return [
            order
            for order in session.order_map.values()
            if order.status in {OrderStatus.SUBMITTING, OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION}
        ]

    async def user_data_stream_health(self, session, credentials):
        return await OrderManager.user_data_stream_health(self, session, credentials)

    def _user_stream_stale_threshold_seconds(self) -> int:
        return OrderManager._user_stream_stale_threshold_seconds(self)

    def mark_runtime_order_update(self) -> None:
        OrderManager._mark_user_stream_order_update_heartbeat(self)

    def _mark_user_stream_order_update_heartbeat(self) -> None:
        OrderManager._mark_user_stream_order_update_heartbeat(self)

    def set_user_stream_primary_path_availability(self, *, available: bool, reason: str | None = None) -> None:
        OrderManager.set_user_stream_primary_path_availability(self, available=available, reason=reason)

    @staticmethod
    def _datetime_from_millis(value):
        return OrderManager._datetime_from_millis(value)

    @staticmethod
    def _decimal_from_payload(value):
        return OrderManager._decimal_from_payload(value)


class StubGateway:
    def __init__(
        self,
        *,
        mark_prices_payload: dict[str, dict[str, str]] | None = None,
        mark_price_payloads: dict[str, dict[str, str]] | None = None,
        risk_state_payload: dict[str, object] | None = None,
        exchange_info_payload: dict[str, object] | None = None,
    ) -> None:
        self.mark_prices_payload = mark_prices_payload or {
            "SKIPUSDT": {"markPrice": "100"},
            "OPENUSDT": {"markPrice": "100"},
            "EXTRAUSDT": {"markPrice": "100"},
        }
        self.mark_price_payloads = mark_price_payloads or {}
        self.mark_price_calls: list[str] = []
        self.klines_calls: list[tuple[str, str, int]] = []
        self.exchange_info_payload = exchange_info_payload or {
            "symbols": [{"symbol": symbol, "status": "TRADING"} for symbol in self.mark_prices_payload]
        }
        self.risk_state_payload = risk_state_payload or {
            "healthy": True,
            "risk_error_streak": 0,
            "threshold": 3,
            "last_error": None,
            "last_error_at": None,
        }

    async def exchange_info(self):
        return self.exchange_info_payload

    def parse_symbol_filters(self, _exchange_info):
        return {
            symbol: SymbolFilters(
                symbol=symbol,
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                min_notional=Decimal("5"),
            )
            for symbol in self.mark_prices_payload
        }

    async def mark_prices(self):
        return self.mark_prices_payload

    async def mark_price(self, symbol: str):
        self.mark_price_calls.append(symbol)
        return self.mark_price_payloads.get(symbol, self.mark_prices_payload.get(symbol, {"markPrice": "100"}))

    async def leverage_brackets(self, _credentials):
        return {}

    async def klines(self, _symbol: str, interval: str, _limit: int):
        self.klines_calls.append((_symbol, interval, _limit))
        base = {
            "15m": 100.0,
            "1h": 100.0,
            "4h": 100.0,
        }[interval]
        candles = []
        for index in range(_limit):
            price = base + (index * 0.1)
            candles.append(
                [
                    index,
                    str(price),
                    str(price + 1),
                    str(price - 1),
                    str(price + 0.5),
                    "1000",
                    index + 1,
                    "0",
                    "0",
                    "0",
                    "0",
                    True,
                ]
            )
        return candles

    def risk_error_state(self):
        return dict(self.risk_state_payload)


class AutoModeTestService(AutoModeService):
    def __init__(self, *, order_manager, gateway, market_health=None) -> None:
        super().__init__(
            scanner_service=None,
            order_manager=order_manager,
            gateway=gateway,
            ws_manager=SimpleNamespace(broadcast=self._broadcast),
            session_factory=None,
            market_health=market_health,
        )
        self.active_auto_orders: list[Order] = []
        self.active_local_orders: list[Order] = []
        self.pending_scores: dict[int, tuple[int, int]] = {}
        self.pending_rank_values: dict[int, float] = {}
        self.actionable_signals: list[Signal] = []
        self.drift_rows: dict[str, dict[str, Decimal | int]] = {}
        self.ready_drift_symbols: list[str] = []
        self.drift_process_calls: list[tuple[int, tuple[str, ...]]] = []
        self.broadcast_calls: list[str] = []

    async def _broadcast(self, event: str, payload: dict) -> None:
        if event == "auto_mode_state_change":
            self.broadcast_calls.append(str(payload.get("reason")))

    async def _mode_is_enabled(self, _session) -> bool:
        return True

    async def _active_orders(self, _session, *, approved_by: str | None = None) -> list[Order]:
        if approved_by == "AUTO_MODE":
            return [order for order in self.active_auto_orders if order.status in {OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION}]
        return [order for order in self.active_local_orders if order.status in {OrderStatus.ORDER_PLACED, OrderStatus.IN_POSITION}]

    async def _ranked_pending_orders(self, _session, *, orders: list[Order]) -> list[RankedPendingOrder]:
        return [
            RankedPendingOrder(
                order=order,
                final_score=self.pending_scores.get(order.id, (0, 0))[0],
                confirmation_score=self.pending_scores.get(order.id, (0, 0))[1],
                rank_value=self.pending_rank_values.get(order.id, float(self.pending_scores.get(order.id, (0, 0))[0])),
            )
            for order in orders
        ]

    async def _actionable_signals_for_cycle(self, _session, *, cycle_id: int) -> list[Signal]:
        return [signal for signal in self.actionable_signals if signal.scan_cycle_id == cycle_id]

    async def _upsert_drift_symbol(self, _session, *, symbol: str, planned_entry_price: Decimal, scan_cycle_id: int | None) -> None:
        self.drift_rows[symbol.upper()] = {
            "planned_entry_price": planned_entry_price,
            "miss_count": 0,
            "scan_cycle_id": Decimal(scan_cycle_id or 0),
        }

    async def _delete_drift_symbol(self, _session, *, symbol: str) -> None:
        self.drift_rows.pop(symbol.upper(), None)

    async def _ready_drift_symbols_for_cycle(self, _session, *, scan_cycle_id: int | None = None) -> list[str]:
        return list(self.ready_drift_symbols)

    async def _process_drift_requalification_results(self, _session, *, scan_cycle_id: int, ready_symbols: list[str]) -> None:
        self.drift_process_calls.append((scan_cycle_id, tuple(ready_symbols)))
        valid_symbols = {
            signal.symbol.upper()
            for signal in self.actionable_signals
            if signal.scan_cycle_id == scan_cycle_id and signal.symbol.upper() in {item.upper() for item in ready_symbols}
        }
        for symbol in ready_symbols:
            row = self.drift_rows.get(symbol.upper())
            if row is None:
                continue
            if symbol.upper() in valid_symbols:
                row["miss_count"] = 0
                continue
            next_miss_count = int(row.get("miss_count", 0)) + 1
            if next_miss_count >= 3:
                self.drift_rows.pop(symbol.upper(), None)
            else:
                row["miss_count"] = next_miss_count


class StubMarketHealth:
    def __init__(self, snapshots: dict[str, dict[str, object]]) -> None:
        self.snapshots = snapshots

    async def snapshot(self, symbol: str):
        payload = self.snapshots.get(symbol.upper(), {})
        return SimpleNamespace(
            symbol=symbol.upper(),
            book_ticker=payload.get("book_ticker"),
            mark_price=payload.get("mark_price"),
            spread_bps=payload.get("spread_bps"),
            spread_median_bps=payload.get("spread_median_bps"),
            spread_relative_ratio=payload.get("spread_relative_ratio"),
            relative_spread_ready=payload.get("relative_spread_ready", False),
            relative_spread_sample_count=payload.get("relative_spread_sample_count", 0),
            book_stable=payload.get("book_stable", True),
            last_updated_at=payload.get("last_updated_at"),
        )


def bind_session_state(session: FakeSession, service: AutoModeTestService, signals: list[Signal] | None = None) -> FakeSession:
    session.order_map = {order.id: order for order in service.active_local_orders}
    session.signal_map = {signal.id: signal for signal in (signals or [])}
    return session


def audit_entries(session: FakeSession, *, event_type: str) -> list[AuditLog]:
    return [
        entry
        for entry in session.added
        if isinstance(entry, AuditLog) and entry.event_type == event_type
    ]


@pytest.mark.asyncio
async def test_build_preview_uses_risk_budget_override_usdt() -> None:
    manager = OrderManager(gateway=None, ws_manager=WebSocketManager(), notifier=DummyNotifier())
    preview = manager.build_preview(
        balance=Decimal("100"),
        settings_map={"risk_per_trade_pct": "1.0", "max_leverage": "10"},
        filters=SymbolFilters(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
        risk_budget_override_usdt=Decimal("20"),
    )

    assert preview["risk_budget_usdt"] == "20"
    assert Decimal(preview["risk_usdt_at_stop"]) > 0
    assert Decimal(preview["risk_usdt_at_stop"]) <= Decimal("20")
    assert Decimal(preview["risk_usdt_at_stop"]) < Decimal("20")


@pytest.mark.asyncio
async def test_build_preview_uses_stop_distance_position_sizing_for_auto_mode() -> None:
    manager = OrderManager(gateway=None, ws_manager=WebSocketManager(), notifier=DummyNotifier())
    preview = manager.build_preview(
        balance=Decimal("100"),
        settings_map={"risk_per_trade_pct": "1.0", "max_leverage": "10"},
        filters=SymbolFilters(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
        risk_budget_override_usdt=Decimal("20"),
        use_stop_distance_position_sizing=True,
    )

    assert preview["risk_budget_usdt"] == "1"
    assert preview["requested_quantity"] == "0.194"
    assert preview["final_quantity"] == "0.194"
    assert Decimal(preview["risk_usdt_at_stop"]) == Decimal("0.996384")


@pytest.mark.asyncio
async def test_build_preview_uses_safe_default_risk_when_setting_is_invalid() -> None:
    manager = OrderManager(gateway=None, ws_manager=WebSocketManager(), notifier=DummyNotifier())
    preview = manager.build_preview(
        balance=Decimal("100"),
        settings_map={"risk_fraction": "invalid", "max_leverage": "10"},
        filters=SymbolFilters(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
        risk_budget_override_usdt=Decimal("100"),
        use_stop_distance_position_sizing=True,
    )

    assert preview["risk_budget_usdt"] == "2"
    assert preview["requested_quantity"] == "0.389"
    assert Decimal(preview["risk_usdt_at_stop"]) == Decimal("1.997904")


@pytest.mark.asyncio
async def test_build_preview_auto_mode_uses_remaining_budget_as_margin_for_small_balances() -> None:
    manager = OrderManager(gateway=None, ws_manager=WebSocketManager(), notifier=DummyNotifier())
    preview = manager.build_preview(
        balance=Decimal("13"),
        settings_map={"risk_per_trade_pct": "1.0", "max_leverage": "10"},
        filters=SymbolFilters(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
        risk_budget_override_usdt=Decimal("4.333333333333333333333333333"),
        use_stop_distance_position_sizing=True,
    )

    assert preview["status"] == "too_small_for_exchange"
    assert preview["can_place"] is False
    assert preview["risk_budget_usdt"] == "0.13"
    assert preview["final_quantity"] == "0"


@pytest.mark.asyncio
async def test_auto_mode_approve_uses_signal_expiry_and_target_risk_override() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = []
    service.active_local_orders = []
    actionable_signal = make_signal(signal_id=10, symbol="OPENUSDT", direction=SignalDirection.LONG, cycle_id=9)
    session = bind_session_state(FakeSession(), service, [actionable_signal])

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map={"risk_per_trade_pct": "1.0", "max_portfolio_risk_pct": "3.0", "max_leverage": "10"},
        actionable_signals=[actionable_signal],
    )

    assert order_manager.approve_calls[0]["validity_hours"] is None
    assert order_manager.approve_calls[0]["expires_at_override"] == actionable_signal.expires_at
    assert order_manager.approve_calls[0]["target_risk_usdt_override"] == Decimal("1")

@pytest.mark.asyncio
async def test_auto_mode_manages_pending_auto_orders_without_closing_live_positions_in_cycle_rebalance() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [
        make_order(order_id=1, symbol="PENDINGUSDT", status=OrderStatus.ORDER_PLACED),
        make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION),
    ]
    service.active_local_orders = list(service.active_auto_orders)
    session = bind_session_state(FakeSession(), service)

    await service._manage_existing_orders(
        session,
        actionable_signals=[make_signal(signal_id=3, symbol="OTHERUSDT", direction=SignalDirection.LONG, cycle_id=7)],
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == []


@pytest.mark.asyncio
async def test_manage_live_positions_closes_on_regime_flip() -> None:
    order_manager = StubOrderManager()
    market_health = StubMarketHealth(
        {
            "OPENUSDT": {
                "book_ticker": {"bidPrice": "99.9", "askPrice": "100.0", "bidQty": "10", "askQty": "10"},
                "spread_bps": 10.0,
                "spread_median_bps": 6.0,
                "spread_relative_ratio": 1.6,
                "relative_spread_ready": True,
                "relative_spread_sample_count": 120,
                "book_stable": True,
            }
        }
    )
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway(), market_health=market_health)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    service.active_auto_orders = [live_order]
    service.active_local_orders = [live_order]
    session = bind_session_state(FakeSession(), service)

    monkeypatcher = pytest.MonkeyPatch()
    monkeypatcher.setattr(
        auto_mode_module,
        "classify_market_state",
        lambda **_kwargs: SimpleNamespace(market_state="BEAR_TREND", direction=SignalDirection.SHORT),
    )
    try:
        await service.manage_live_positions(session)
    finally:
        monkeypatcher.undo()

    assert order_manager.close_calls == [(2, "aqrr_regime_flip")]


@pytest.mark.asyncio
async def test_manage_live_positions_closes_after_consecutive_spread_deterioration() -> None:
    order_manager = StubOrderManager()
    market_health = StubMarketHealth(
        {
            "OPENUSDT": {
                "book_ticker": {"bidPrice": "99.5", "askPrice": "100.5", "bidQty": "10", "askQty": "10"},
                "spread_bps": 100.0,
                "spread_median_bps": 20.0,
                "spread_relative_ratio": 5.0,
                "relative_spread_ready": True,
                "relative_spread_sample_count": 120,
                "book_stable": True,
            }
        }
    )
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway(), market_health=market_health)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    service.active_auto_orders = [live_order]
    service.active_local_orders = [live_order]
    session = bind_session_state(FakeSession(), service)

    monkeypatcher = pytest.MonkeyPatch()
    monkeypatcher.setattr(
        auto_mode_module,
        "classify_market_state",
        lambda **_kwargs: SimpleNamespace(market_state="BULL_TREND", direction=SignalDirection.LONG),
    )
    try:
        await service.manage_live_positions(session)
        await service.manage_live_positions(session)
    finally:
        monkeypatcher.undo()

    assert order_manager.close_calls == [(2, "aqrr_spread_deteriorated")]


@pytest.mark.asyncio
async def test_live_position_invalidation_uses_30_day_15m_window_for_volatility_state() -> None:
    order_manager = StubOrderManager()
    gateway = StubGateway(mark_prices_payload={"OPENUSDT": {"markPrice": "100", "indexPrice": "100"}})
    service = AutoModeTestService(order_manager=order_manager, gateway=gateway)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    config = resolve_strategy_config(AUTO_MODE_SETTINGS)

    reason = await service._live_position_invalidation_reason(order=live_order, config=config)

    assert reason is None
    fifteen_minute_calls = [call for call in gateway.klines_calls if call[1] == "15m"]
    assert len(fifteen_minute_calls) == 1
    assert fifteen_minute_calls[0][2] == required_15m_candles_for_volatility_shock(atr_period=config.atr_period_15m)


@pytest.mark.asyncio
async def test_auto_mode_emergency_guard_flattens_and_halts_on_broken_user_stream() -> None:
    order_manager = StubOrderManager()
    order_manager.user_stream_health_payload = {
        "healthy": False,
        "required": True,
        "mode": "user_stream",
        "streak": 4,
    }
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == [(2, "auto_mode_emergency_flatten")]
    assert order_manager.approve_calls == []
    emergency_audits = audit_entries(session, event_type="AUTO_MODE_EMERGENCY_GUARD_TRIGGERED")
    assert len(emergency_audits) == 1
    assert emergency_audits[0].details["reason"] == "user_stream_unreliable"


@pytest.mark.asyncio
async def test_auto_mode_emergency_guard_flattens_and_halts_on_runtime_user_stream_liveness_failure() -> None:
    order_manager = RuntimeUserStreamOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == [(2, "auto_mode_emergency_flatten")]
    assert order_manager.approve_calls == []
    emergency_audits = audit_entries(session, event_type="AUTO_MODE_EMERGENCY_GUARD_TRIGGERED")
    assert len(emergency_audits) == 1
    assert emergency_audits[0].details["reason"] == "user_stream_unreliable"
    assert emergency_audits[0].details["mode"] == "authoritative_order_update_liveness"
    assert emergency_audits[0].details["health_reason"] == "stale_order_update_liveness"


@pytest.mark.asyncio
async def test_runtime_user_stream_liveness_health_recovers_after_heartbeat() -> None:
    order_manager = RuntimeUserStreamOrderManager()
    session = FakeSession()
    session.order_map = {
        1: make_order(order_id=1, symbol="OPENUSDT", status=OrderStatus.IN_POSITION),
    }
    credentials = await order_manager.get_credentials(session)

    unhealthy = await order_manager.user_data_stream_health(session, credentials)
    assert unhealthy["required"] is True
    assert unhealthy["healthy"] is False
    assert unhealthy["health_reason"] == "stale_order_update_liveness"

    order_manager.mark_runtime_order_update()
    healthy = await order_manager.user_data_stream_health(session, credentials)
    assert healthy["required"] is True
    assert healthy["healthy"] is True
    assert healthy["health_reason"] == "healthy"


@pytest.mark.asyncio
async def test_runtime_user_stream_liveness_health_recovers_after_order_trade_update_event() -> None:
    order_manager = RuntimeUserStreamOrderManager()
    session = FakeSession()
    session.order_map = {
        1: make_order(order_id=1, symbol="OPENUSDT", status=OrderStatus.IN_POSITION),
    }
    credentials = await order_manager.get_credentials(session)

    unhealthy = await order_manager.user_data_stream_health(session, credentials)
    assert unhealthy["required"] is True
    assert unhealthy["healthy"] is False

    OrderManager.handle_user_stream_event(
        order_manager,
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": int(datetime.now(timezone.utc).timestamp() * 1000),
            "o": {
                "s": "OPENUSDT",
                "X": "PARTIALLY_FILLED",
                "x": "TRADE",
            },
        },
    )
    healthy = await order_manager.user_data_stream_health(session, credentials)
    assert healthy["required"] is True
    assert healthy["healthy"] is True
    assert healthy["health_reason"] == "healthy"


@pytest.mark.asyncio
async def test_runtime_user_stream_health_falls_back_to_polling_when_event_stream_is_unavailable() -> None:
    order_manager = RuntimeUserStreamOrderManager()
    order_manager._user_stream_primary_available = False
    order_manager._user_stream_primary_reason = "event_stream_disconnected"
    session = FakeSession()
    session.order_map = {
        1: make_order(order_id=1, symbol="OPENUSDT", status=OrderStatus.IN_POSITION),
    }
    credentials = await order_manager.get_credentials(session)

    payload = await order_manager.user_data_stream_health(session, credentials)
    assert payload["required"] is True
    assert payload["healthy"] is True
    assert payload["mode"] == "polling_fallback"
    assert payload["health_reason"] == "event_stream_unavailable"


@pytest.mark.asyncio
async def test_auto_mode_emergency_guard_keeps_polling_fallback_when_event_stream_is_unavailable() -> None:
    order_manager = StubOrderManager()
    order_manager.user_stream_health_payload = {
        "healthy": True,
        "required": True,
        "mode": "polling_fallback",
        "health_reason": "event_stream_unavailable",
    }
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION)
    service.active_auto_orders = [pending_order, live_order]
    session = bind_session_state(FakeSession(), service)
    credentials = await order_manager.get_credentials(session)
    account_snapshot = await order_manager.get_account_snapshot(session, credentials)

    emergency_state = await service._emergency_safety_state(
        session,
        active_auto_orders=[pending_order, live_order],
        account_snapshot=account_snapshot,
    )

    assert emergency_state.active is False


@pytest.mark.asyncio
async def test_account_update_event_updates_read_account_snapshot_for_emergency_checks() -> None:
    class AccountInfoGateway:
        async def account_info(self, _credentials):
            return {
                "totalWalletBalance": "50",
                "availableBalance": "50",
            }

    order_manager = OrderManager(
        gateway=AccountInfoGateway(),
        ws_manager=WebSocketManager(),
        notifier=DummyNotifier(),
    )
    order_manager.set_user_stream_primary_path_availability(available=True, reason="runtime_test")
    order_manager.handle_user_stream_event(
        {
            "e": "ACCOUNT_UPDATE",
            "a": {
                "m": "ORDER",
                "B": [{"a": "USDT", "wb": "120", "cw": "-3"}],
                "P": [{"s": "OPENUSDT", "iw": "15"}],
            },
        }
    )

    snapshot = await order_manager.get_read_account_snapshot(
        session=None,
        credentials=SimpleNamespace(api_key="key", private_key_pem="private"),
    )

    assert snapshot.wallet_balance == Decimal("120")
    assert snapshot.available_balance == Decimal("-3")
    assert snapshot.total_position_initial_margin == Decimal("15")


@pytest.mark.asyncio
async def test_auto_mode_emergency_guard_flattens_on_order_update_integrity_failure() -> None:
    order_manager = StubOrderManager()
    order_manager.order_update_integrity_payload = {
        "healthy": False,
        "failure_count": 5,
        "threshold": 3,
        "lookback_minutes": 15,
    }
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == [(2, "auto_mode_emergency_flatten")]
    assert order_manager.approve_calls == []
    emergency_audits = audit_entries(session, event_type="AUTO_MODE_EMERGENCY_GUARD_TRIGGERED")
    assert len(emergency_audits) == 1
    assert emergency_audits[0].details["reason"] == "order_update_integrity_broken"


@pytest.mark.asyncio
async def test_auto_mode_emergency_guard_triggers_on_repeated_exchange_risk_errors() -> None:
    order_manager = StubOrderManager()
    gateway = StubGateway(
        risk_state_payload={
            "healthy": False,
            "risk_error_streak": 4,
            "threshold": 3,
            "last_error": "reduceOnly rejected",
            "last_error_at": "2026-01-01T00:00:00+00:00",
        }
    )
    service = AutoModeTestService(order_manager=order_manager, gateway=gateway)
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    session = bind_session_state(FakeSession(), service)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=[],
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == [(2, "auto_mode_emergency_flatten")]
    emergency_audits = audit_entries(session, event_type="AUTO_MODE_EMERGENCY_GUARD_TRIGGERED")
    assert len(emergency_audits) == 1
    assert emergency_audits[0].details["reason"] == "repeated_exchange_risk_errors"


@pytest.mark.asyncio
async def test_auto_mode_emergency_guard_triggers_on_abnormal_mark_price_behavior() -> None:
    order_manager = StubOrderManager()
    gateway = StubGateway(
        mark_prices_payload={
            "PENDUSDT": {"markPrice": "130", "indexPrice": "100"},
            "OPENUSDT": {"markPrice": "130", "indexPrice": "100"},
            "NEWUSDT": {"markPrice": "100", "indexPrice": "100"},
        }
    )
    service = AutoModeTestService(order_manager=order_manager, gateway=gateway)
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == [(2, "auto_mode_emergency_flatten")]
    assert order_manager.approve_calls == []
    emergency_audits = audit_entries(session, event_type="AUTO_MODE_EMERGENCY_GUARD_TRIGGERED")
    assert len(emergency_audits) == 1
    assert emergency_audits[0].details["reason"] == "mark_price_abnormality"
    assert emergency_audits[0].details["affected_symbols"][0]["issue"] == "mark_price_deviation_exceeded"


@pytest.mark.asyncio
async def test_auto_mode_emergency_guard_triggers_on_symbol_suspension_or_delisting() -> None:
    order_manager = StubOrderManager()
    gateway = StubGateway(
        mark_prices_payload={
            "PENDUSDT": {"markPrice": "100", "indexPrice": "100"},
            "OPENUSDT": {"markPrice": "100", "indexPrice": "100"},
            "NEWUSDT": {"markPrice": "100", "indexPrice": "100"},
        },
        exchange_info_payload={
            "symbols": [
                {"symbol": "PENDUSDT", "status": "TRADING"},
                {"symbol": "OPENUSDT", "status": "SETTLING"},
                {"symbol": "NEWUSDT", "status": "TRADING"},
            ]
        },
    )
    service = AutoModeTestService(order_manager=order_manager, gateway=gateway)
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED)
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == [(2, "auto_mode_emergency_flatten")]
    assert order_manager.approve_calls == []
    emergency_audits = audit_entries(session, event_type="AUTO_MODE_EMERGENCY_GUARD_TRIGGERED")
    assert len(emergency_audits) == 1
    assert emergency_audits[0].details["reason"] == "symbol_suspension_or_delisting"
    assert emergency_audits[0].details["affected_symbols"][0]["symbol"] == "OPENUSDT"
    assert emergency_audits[0].details["affected_symbols"][0]["status"] == "SETTLING"


@pytest.mark.asyncio
async def test_manage_existing_orders_invalidates_pending_on_new_open_correlation_conflict() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED, direction=SignalDirection.LONG)
    pending_order.setup_family = "pullback_continuation"
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    session = bind_session_state(FakeSession(), service)
    session.scan_result_rows = []

    async def fake_returns(*, symbol: str, cache: dict[str, list[float]]) -> list[float]:
        if symbol.upper() == "PENDUSDT":
            return [0.01, 0.02, 0.03, 0.04, 0.05]
        if symbol.upper() == "OPENUSDT":
            return [0.011, 0.021, 0.031, 0.041, 0.051]
        return [0.0, 0.0, 0.0]

    service._returns_1h = fake_returns  # type: ignore[assignment]

    await service._manage_existing_orders(
        session,
        actionable_signals=[],
        scan_cycle_id=9,
    )

    assert order_manager.cancel_calls == [(1, "correlation_conflict")]
    assert order_manager.cancel_reason_contexts[0]["invalidation_scope"] == "pending_vs_open"


@pytest.mark.asyncio
async def test_manage_existing_orders_fails_safe_when_pending_open_correlation_data_is_insufficient() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED, direction=SignalDirection.LONG)
    pending_order.setup_family = "pullback_continuation"
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    service.active_auto_orders = [pending_order, live_order]
    service.active_local_orders = [pending_order, live_order]
    session = bind_session_state(FakeSession(), service)
    session.scan_result_rows = []

    async def fake_returns(*, symbol: str, cache: dict[str, list[float]]) -> list[float]:
        if symbol.upper() == "PENDUSDT":
            return [0.01, 0.02]
        return [0.01, 0.02, 0.03, 0.04]

    service._returns_1h = fake_returns  # type: ignore[assignment]

    await service._manage_existing_orders(
        session,
        actionable_signals=[],
        scan_cycle_id=9,
    )

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.cancel_reason_contexts[0]["invalidation_scope"] == "pending_vs_open"
    assert order_manager.cancel_reason_contexts[0]["reason"] == "correlation_guard_unavailable"
    assert order_manager.cancel_reason_contexts[0]["guard_failure"] == "insufficient_pending_returns"


@pytest.mark.asyncio
async def test_manage_existing_orders_invalidates_same_direction_pending_when_setup_state_changes() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDUSDT", status=OrderStatus.ORDER_PLACED, direction=SignalDirection.LONG)
    pending_order.setup_family = "breakout_retest"
    pending_order.setup_variant = "trend_breakout_retest"
    pending_order.entry_style = "LIMIT_GTD"
    service.active_auto_orders = [pending_order]
    service.active_local_orders = [pending_order]
    fresh_same_direction = make_signal(signal_id=11, symbol="PENDUSDT", direction=SignalDirection.LONG, cycle_id=9)
    fresh_same_direction.setup_family = "pullback_continuation"
    fresh_same_direction.setup_variant = "trend_pullback_continuation"
    fresh_same_direction.entry_style = "LIMIT_GTD"
    session = bind_session_state(FakeSession(), service, [fresh_same_direction])
    session.scan_result_rows = []

    await service._manage_existing_orders(
        session,
        actionable_signals=[fresh_same_direction],
        scan_cycle_id=9,
    )

    assert order_manager.cancel_calls == [(1, "setup_state_changed")]
    assert order_manager.cancel_reason_contexts[0]["same_direction_setup_refresh_required"] is True
    assert order_manager.cancel_reason_contexts[0]["fresh_signal_ids"] == [11]


@pytest.mark.parametrize(
    ("setup_family", "filter_reasons", "extra_context", "expected_reason"),
    [
        (
            "breakout_retest",
            [],
            {"selection_rejection_reason": "correlation_conflict"},
            "correlation_conflict",
        ),
        (
            "pullback_continuation",
            ["spread_above_threshold"],
            {},
            "spread_filter_failed",
        ),
        (
            "pullback_continuation",
            [],
            {"volatility_shock": True},
            "volatility_shock",
        ),
        (
            "pullback_continuation",
            ["unstable_no_trade"],
            {"market_state": "UNSTABLE"},
            "regime_flipped",
        ),
        (
            "breakout_retest",
            [],
            {"market_state": "BALANCED_RANGE"},
            "setup_state_changed",
        ),
        (
            "range_reversion",
            ["range_structure_break"],
            {},
            "structure_invalidated",
        ),
        (
            "pullback_continuation",
            ["aqrr_hard_filters_failed"],
            {},
            "viability_lost",
        ),
        (
            "pullback_continuation",
            [],
            {"selection_rejection_reason": "slot_limit_reached"},
            "capacity_rejected",
        ),
    ],
)
def test_pending_invalidation_reason_maps_aqrr_specific_failures(
    setup_family: str,
    filter_reasons: list[str],
    extra_context: dict[str, object],
    expected_reason: str,
) -> None:
    order = make_order(order_id=1, symbol="BTCUSDT", status=OrderStatus.ORDER_PLACED)
    order.setup_family = setup_family
    scan_result = SimpleNamespace(
        symbol="BTCUSDT",
        filter_reasons=filter_reasons,
        extra_context=extra_context,
    )

    assert AutoModeService._pending_invalidation_reason(order=order, scan_result=scan_result) == expected_reason


def test_pending_invalidation_decision_preserves_raw_aqrr_reason() -> None:
    order = make_order(order_id=1, symbol="BTCUSDT", status=OrderStatus.ORDER_PLACED)
    order.setup_family = "pullback_continuation"
    scan_result = SimpleNamespace(
        symbol="BTCUSDT",
        filter_reasons=["pullback_no_rejection_evidence"],
        extra_context={
            "aqrr_raw_rejection_reason": "pullback_no_rejection_evidence",
            "aqrr_raw_rejection_reasons": ["pullback_no_rejection_evidence"],
            "aqrr_rejection_stage": "candidate_build",
        },
    )

    decision = AutoModeService._pending_invalidation_decision(order=order, scan_result=scan_result)

    assert decision.lifecycle_reason == "viability_lost"
    assert decision.raw_aqrr_reason == "pullback_no_rejection_evidence"
    assert decision.raw_aqrr_reasons == ("pullback_no_rejection_evidence",)
    assert decision.aqrr_rejection_stage == "candidate_build"


@pytest.mark.asyncio
async def test_manage_existing_orders_preserves_raw_reason_context_when_mapping_to_lifecycle_reason() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDINGUSDT", status=OrderStatus.ORDER_PLACED)
    pending_order.setup_family = "pullback_continuation"
    service.active_auto_orders = [pending_order]
    service.active_local_orders = [pending_order]
    session = bind_session_state(FakeSession(), service)
    session.scan_result_rows = [
        SimpleNamespace(
            symbol="PENDINGUSDT",
            direction=SignalDirection.LONG,
            filter_reasons=["pullback_no_rejection_evidence"],
            extra_context={
                "market_state": "BULL_TREND",
                "aqrr_raw_rejection_reason": "pullback_no_rejection_evidence",
                "aqrr_raw_rejection_reasons": ["pullback_no_rejection_evidence"],
                "aqrr_rejection_stage": "candidate_build",
            },
        )
    ]

    await service._manage_existing_orders(session, actionable_signals=[], scan_cycle_id=7)

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.cancel_reason_contexts[0] == {
        "lifecycle_reason": "viability_lost",
        "raw_aqrr_reason": "pullback_no_rejection_evidence",
        "raw_aqrr_reasons": ["pullback_no_rejection_evidence"],
        "aqrr_rejection_stage": "candidate_build",
    }


@pytest.mark.asyncio
async def test_manage_existing_orders_does_not_cancel_pending_entry_on_slot_pressure_rejection() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="PENDINGUSDT", status=OrderStatus.ORDER_PLACED)
    pending_order.setup_family = "pullback_continuation"
    service.active_auto_orders = [pending_order]
    service.active_local_orders = [pending_order]
    session = bind_session_state(FakeSession(), service)
    session.scan_result_rows = [
        SimpleNamespace(
            symbol="PENDINGUSDT",
            direction=SignalDirection.LONG,
            filter_reasons=[],
            extra_context={
                "market_state": "BULL_TREND",
                "selection_rejection_reason": "slot_limit_reached",
            },
        )
    ]

    await service._manage_existing_orders(session, actionable_signals=[], scan_cycle_id=7)

    assert order_manager.cancel_calls == []
    retained = audit_entries(session, event_type="AUTO_MODE_PENDING_RETAINED")
    assert len(retained) == 1
    assert retained[0].details["reason"] == "capacity_rejected"
    assert retained[0].details["selection_rejection_reason"] == "slot_limit_reached"


@pytest.mark.asyncio
async def test_auto_mode_rebalance_rejects_new_open_on_open_position_correlation_conflict() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(
        order_manager=order_manager,
        gateway=StubGateway(
            mark_prices_payload={
                "OPENUSDT": {"markPrice": "100"},
                "NEWUSDT": {"markPrice": "100"},
            }
        ),
    )
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    service.active_auto_orders = [live_order]
    service.active_local_orders = [live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].extra_context = {"strategy_key": "aqrr_binance_usdm"}
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    skipped = audit_entries(session, event_type="AUTO_MODE_ORDER_SKIPPED")
    assert order_manager.approve_calls == []
    assert len(skipped) == 1
    assert skipped[0].details["reason"] == "correlation_conflict"
    assert skipped[0].details["conflict_symbol"] == "OPENUSDT"


@pytest.mark.asyncio
async def test_auto_mode_rebalance_blocks_new_open_when_correlation_guard_raises_exception() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    service.active_auto_orders = [live_order]
    service.active_local_orders = [live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].extra_context = {"strategy_key": "aqrr_binance_usdm"}
    session = bind_session_state(FakeSession(), service, actionable_signals)

    async def raising_returns(*, symbol: str, cache: dict[str, list[float]]) -> list[float]:
        raise RuntimeError("returns unavailable")

    service._returns_1h = raising_returns  # type: ignore[assignment]

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    skipped = audit_entries(session, event_type="AUTO_MODE_ORDER_SKIPPED")
    assert order_manager.approve_calls == []
    assert len(skipped) == 1
    assert skipped[0].details["reason"] == "correlation_guard_unavailable"
    assert skipped[0].details["guard_failure"] == "exception"


@pytest.mark.asyncio
async def test_auto_mode_rebalance_blocks_new_open_when_correlation_context_is_missing() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    live_order = make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, direction=SignalDirection.LONG)
    service.active_auto_orders = [live_order]
    service.active_local_orders = [live_order]
    actionable_signals = [make_signal(signal_id=10, symbol="NEWUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].extra_context = {}
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    skipped = audit_entries(session, event_type="AUTO_MODE_ORDER_SKIPPED")
    assert order_manager.approve_calls == []
    assert len(skipped) == 1
    assert skipped[0].details["reason"] == "correlation_guard_unavailable"
    assert skipped[0].details["guard_failure"] == "missing_signal_context"


@pytest.mark.asyncio
async def test_auto_mode_rebalance_respects_slot_limit_and_skips_symbols_with_active_local_orders() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [
        make_order(order_id=1, symbol="AUTO1USDT", status=OrderStatus.ORDER_PLACED),
        make_order(order_id=2, symbol="AUTO2USDT", status=OrderStatus.IN_POSITION),
    ]
    service.active_local_orders = service.active_auto_orders + [make_order(order_id=3, symbol="SKIPUSDT", status=OrderStatus.ORDER_PLACED, approved_by="LEGACY_MODE")]
    service.pending_scores = {1: (95, 80)}

    actionable_signals = [
        make_signal(signal_id=10, symbol="SKIPUSDT", direction=SignalDirection.LONG, cycle_id=9),
        make_signal(signal_id=11, symbol="OPENUSDT", direction=SignalDirection.LONG, cycle_id=9),
        make_signal(signal_id=12, symbol="EXTRAUSDT", direction=SignalDirection.LONG, cycle_id=9),
    ]
    session = bind_session_state(FakeSession(), service, actionable_signals)
    scan_cycle = ScanCycle(
        id=9,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        status=ScanStatus.COMPLETE,
        symbols_scanned=3,
        candidates_found=3,
        signals_qualified=3,
        trigger_type=TriggerType.AUTO_MODE,
        progress_pct=100,
    )

    await service._rebalance_pending_orders(
        session,
        scan_cycle=scan_cycle,
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.approve_calls == []
    assert order_manager.cancel_calls == []


@pytest.mark.asyncio
async def test_auto_mode_rebalance_opens_three_symbols_when_slots_are_available() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = []
    service.active_local_orders = []
    actionable_signals = [
        make_signal(signal_id=10, symbol="OPEN1USDT", direction=SignalDirection.LONG, cycle_id=9),
        make_signal(signal_id=11, symbol="OPEN2USDT", direction=SignalDirection.SHORT, cycle_id=9),
        make_signal(signal_id=12, symbol="OPEN3USDT", direction=SignalDirection.LONG, cycle_id=9),
    ]
    actionable_signals[0].final_score = 120
    actionable_signals[1].final_score = 110
    actionable_signals[2].final_score = 100
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert [call["signal_id"] for call in order_manager.approve_calls] == [10, 11, 12]
    assert order_manager.cancel_calls == []


@pytest.mark.asyncio
async def test_auto_mode_rebalance_uses_remaining_slot_budget_for_low_balance_open() -> None:
    order_manager = StubOrderManager()
    order_manager.available_balance = Decimal("10")
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway(mark_prices_payload={"JTOUSDT": {"markPrice": "1"}}))
    existing_order = make_order(
        order_id=1,
        symbol="LOCKEDUSDT",
        status=OrderStatus.IN_POSITION,
        risk_usdt_at_stop=Decimal("0.1"),
        direction=SignalDirection.SHORT,
    )
    service.active_auto_orders = [existing_order]
    service.active_local_orders = [existing_order]
    actionable_signals = [make_signal(signal_id=10, symbol="JTOUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].entry_price = Decimal("1")
    actionable_signals[0].stop_loss = Decimal("0.8")
    actionable_signals[0].take_profit = Decimal("1.6")
    actionable_signals[0].extra_context = {"strategy_key": "aqrr_binance_usdm"}
    order_manager.readiness_by_signal_id[10] = {
        "mark_price": Decimal("1"),
        "order_preview": {
            "status": "affordable",
            "can_place": True,
            "auto_resized": False,
            "requested_quantity": "5",
            "final_quantity": "5",
            "max_affordable_quantity": "5",
            "mark_price_used": "1",
            "entry_notional": "5",
                "required_initial_margin": "1",
                "estimated_entry_fee": "0.01",
                "available_balance": "10",
                "reserve_balance": "1",
                "usable_balance": "9",
                "risk_budget_usdt": "0.2",
                "risk_usdt_at_stop": "0.1",
                "recommended_leverage": 5,
                "reason": None,
            },
        "can_open_now": True,
        "failure_reason": None,
    }
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert [call["signal_id"] for call in order_manager.approve_calls] == [10]
    assert order_manager.approve_calls[0]["risk_budget_override_usdt"] == Decimal("4.50")


@pytest.mark.asyncio
async def test_auto_mode_opens_signal_when_entry_is_within_default_three_percent_guard() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = []
    service.active_local_orders = []
    actionable_signals = [make_signal(signal_id=10, symbol="OPENUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    order_manager.readiness_by_signal_id[10] = {
        "mark_price": Decimal("102.46"),
        "order_preview": {
            "status": "affordable",
            "can_place": True,
            "auto_resized": False,
            "requested_quantity": "1",
            "final_quantity": "1",
            "max_affordable_quantity": "1",
            "mark_price_used": "102.46",
            "entry_notional": "100",
            "required_initial_margin": "20",
            "estimated_entry_fee": "0.1",
            "available_balance": "100",
            "reserve_balance": "10",
            "usable_balance": "90",
            "risk_budget_usdt": "2",
            "risk_usdt_at_stop": "1",
            "recommended_leverage": 5,
            "reason": None,
        },
        "can_open_now": True,
        "failure_reason": None,
    }
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert [call["signal_id"] for call in order_manager.approve_calls] == [10]
    assert audit_entries(session, event_type="AUTO_MODE_ORDER_SKIPPED") == []


@pytest.mark.asyncio
async def test_auto_mode_skips_opening_signal_when_entry_is_too_far_from_mark() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = []
    service.active_local_orders = []
    actionable_signals = [make_signal(signal_id=10, symbol="OPENUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    order_manager.readiness_by_signal_id[10] = {
        "mark_price": Decimal("105.01"),
        "order_preview": {
            "status": "affordable",
            "can_place": True,
            "auto_resized": False,
            "requested_quantity": "1",
            "final_quantity": "1",
            "max_affordable_quantity": "1",
            "mark_price_used": "105.01",
            "entry_notional": "100",
            "required_initial_margin": "20",
            "estimated_entry_fee": "0.1",
            "available_balance": "100",
            "reserve_balance": "10",
            "usable_balance": "90",
            "risk_budget_usdt": "26.67",
            "risk_usdt_at_stop": "20",
            "recommended_leverage": 5,
            "reason": None,
        },
        "can_open_now": True,
        "failure_reason": None,
    }
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    skip_audits = audit_entries(session, event_type="AUTO_MODE_ORDER_SKIPPED")

    assert order_manager.approve_calls == []
    assert order_manager.cancel_calls == []
    assert len(skip_audits) == 1
    assert skip_audits[0].details["reason"] == "entry_too_far_from_mark"
    assert "5.01% away from entry" in (skip_audits[0].message or "")


@pytest.mark.asyncio
async def test_auto_mode_records_audit_when_live_readiness_blocks_opening() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = []
    service.active_local_orders = []
    blocked_signal = make_signal(signal_id=10, symbol="BLOCKEDUSDT", direction=SignalDirection.LONG, cycle_id=9)
    blocked_signal.extra_context = {
        "aqrr_raw_rejection_reason": "pullback_no_rejection_evidence",
        "aqrr_raw_rejection_reasons": ["pullback_no_rejection_evidence"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "pullback_continuation",
        "entry_style": "LIMIT_GTD",
    }
    actionable_signals = [blocked_signal]
    order_manager.readiness_by_signal_id[10] = {
        "mark_price": None,
        "order_preview": {"can_place": False, "reason": "preview blocked"},
        "can_open_now": False,
        "failure_reason": "BLOCKEDUSDT live Binance filters are unavailable right now.",
    }
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    skip_audits = audit_entries(session, event_type="AUTO_MODE_ORDER_SKIPPED")

    assert order_manager.approve_calls == []
    assert len(skip_audits) == 1
    assert skip_audits[0].message == "BLOCKEDUSDT live Binance filters are unavailable right now."
    assert skip_audits[0].details["reason"] == "live_readiness_failed"
    assert skip_audits[0].details["failure_reason"] == "BLOCKEDUSDT live Binance filters are unavailable right now."
    assert skip_audits[0].details["preview_reason"] == "preview blocked"
    assert skip_audits[0].details["raw_aqrr_reason"] == "pullback_no_rejection_evidence"
    assert skip_audits[0].details["raw_aqrr_reasons"] == ["pullback_no_rejection_evidence"]
    assert skip_audits[0].details["aqrr_rejection_stage"] == "candidate_build"


@pytest.mark.asyncio
async def test_auto_mode_cancels_pending_order_when_entry_drifts_too_far_from_mark() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="SKIPUSDT", status=OrderStatus.ORDER_PLACED)
    pending_order.entry_price = Decimal("94")
    service.active_auto_orders = [pending_order]
    service.active_local_orders = [pending_order]
    session = bind_session_state(FakeSession(), service)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=[],
    )

    assert order_manager.cancel_calls == [(1, "setup_state_changed")]
    assert order_manager.approve_calls == []
    assert service.drift_rows["SKIPUSDT"]["planned_entry_price"] == Decimal("94")
    assert service.drift_rows["SKIPUSDT"]["miss_count"] == 0


@pytest.mark.asyncio
async def test_auto_mode_rebalance_uses_fresh_mark_for_pending_drift_cancellation() -> None:
    order_manager = StubOrderManager()
    gateway = StubGateway(
        mark_prices_payload={"SKIPUSDT": {"markPrice": "95"}},
        mark_price_payloads={"SKIPUSDT": {"markPrice": "100"}},
    )
    service = AutoModeTestService(order_manager=order_manager, gateway=gateway)
    pending_order = make_order(order_id=1, symbol="SKIPUSDT", status=OrderStatus.ORDER_PLACED)
    pending_order.entry_price = Decimal("94")
    service.active_auto_orders = [pending_order]
    service.active_local_orders = [pending_order]
    session = bind_session_state(FakeSession(), service)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=[],
    )

    assert order_manager.cancel_calls == [(1, "setup_state_changed")]
    assert gateway.mark_price_calls == ["SKIPUSDT"]


@pytest.mark.asyncio
async def test_auto_mode_keeps_pending_order_when_entry_is_within_distance_guard() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    pending_order = make_order(order_id=1, symbol="SKIPUSDT", status=OrderStatus.ORDER_PLACED)
    pending_order.entry_price = Decimal("99")
    service.active_auto_orders = [pending_order]
    service.active_local_orders = [pending_order]
    service.pending_scores = {1: (95, 80)}
    session = bind_session_state(FakeSession(), service)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=[],
    )

    assert order_manager.cancel_calls == []
    assert order_manager.approve_calls == []


@pytest.mark.asyncio
async def test_auto_mode_reopen_clears_drift_tracking_after_successful_requalification() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = []
    service.active_local_orders = []
    service.drift_rows = {"OPENUSDT": {"planned_entry_price": Decimal("100"), "miss_count": 0}}
    actionable_signal = make_signal(signal_id=10, symbol="OPENUSDT", direction=SignalDirection.LONG, cycle_id=9)
    actionable_signal.extra_context = {"drift_requalification": True}
    session = bind_session_state(FakeSession(), service, [actionable_signal])

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=[actionable_signal],
    )

    assert [call["signal_id"] for call in order_manager.approve_calls] == [10]
    assert "OPENUSDT" not in service.drift_rows


@pytest.mark.asyncio
async def test_auto_mode_ready_drift_symbols_use_configured_distance_guard(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {"auto_mode_max_entry_drift_pct": "5.0"}

    class DriftSession(FakeSession):
        def __init__(self, rows) -> None:
            super().__init__()
            self.rows = rows

        async def execute(self, _statement):
            return FakeExecuteResult(self.rows)

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    session = DriftSession(
        [
            SimpleNamespace(
                symbol="DRIFTUSDT",
                planned_entry_price=Decimal("97.1"),
                miss_count=0,
                last_cancelled_at=datetime.now(timezone.utc),
            )
        ]
    )
    service = AutoModeService(
        scanner_service=None,
        order_manager=StubOrderManager(),
        gateway=StubGateway(mark_prices_payload={"DRIFTUSDT": {"markPrice": "100"}}),
        ws_manager=WebSocketManager(),
        session_factory=None,
    )

    ready_symbols = await service._ready_drift_symbols_for_cycle(session)

    assert ready_symbols == ["DRIFTUSDT"]


@pytest.mark.asyncio
async def test_auto_mode_ready_drift_symbols_prefer_fresh_mark_over_snapshot(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {"auto_mode_max_entry_drift_pct": "5.0"}

    class DriftSession(FakeSession):
        def __init__(self, rows) -> None:
            super().__init__()
            self.rows = rows

        async def execute(self, _statement):
            return FakeExecuteResult(self.rows)

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    session = DriftSession(
        [
            SimpleNamespace(
                symbol="DRIFTUSDT",
                planned_entry_price=Decimal("97.1"),
                miss_count=0,
                last_cancelled_at=datetime.now(timezone.utc),
            )
        ]
    )
    gateway = StubGateway(
        mark_prices_payload={"DRIFTUSDT": {"markPrice": "110"}},
        mark_price_payloads={"DRIFTUSDT": {"markPrice": "100"}},
    )
    service = AutoModeService(
        scanner_service=None,
        order_manager=StubOrderManager(),
        gateway=gateway,
        ws_manager=WebSocketManager(),
        session_factory=None,
    )

    ready_symbols = await service._ready_drift_symbols_for_cycle(session)

    assert ready_symbols == ["DRIFTUSDT"]
    assert gateway.mark_price_calls == ["DRIFTUSDT"]


@pytest.mark.asyncio
async def test_auto_mode_run_cycle_passes_ready_drift_symbols_into_priority_scan(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.ready_drift_symbols = ["DRIFTUSDT"]
    service.drift_rows = {"DRIFTUSDT": {"planned_entry_price": Decimal("100"), "miss_count": 2}}
    service.actionable_signals = [make_signal(signal_id=10, symbol="DRIFTUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    observed_priority_symbols: list[str] = []

    async def run_scan(session, *, trigger_type, priority_symbols=None):
        observed_priority_symbols.extend(priority_symbols or [])
        cycle = ScanCycle(
            id=9,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status=ScanStatus.COMPLETE,
            symbols_scanned=0,
            candidates_found=0,
            signals_qualified=0,
            trigger_type=trigger_type,
            progress_pct=100,
        )
        session.add(cycle)
        return cycle

    session = bind_session_state(FakeSession(), service, service.actionable_signals)
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    service.session_factory = lambda: DummyAsyncContext(session)

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    result = await service.run_cycle(reason="interval")

    assert result is True
    assert observed_priority_symbols == ["DRIFTUSDT"]
    assert service.drift_rows["DRIFTUSDT"]["miss_count"] == 0
    assert service.drift_process_calls == [(9, ("DRIFTUSDT",))]


@pytest.mark.asyncio
async def test_auto_mode_run_cycle_expires_drift_symbol_after_three_failed_requalify_cycles(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.ready_drift_symbols = ["DRIFTUSDT"]
    service.drift_rows = {"DRIFTUSDT": {"planned_entry_price": Decimal("100"), "miss_count": 2}}
    observed_priority_symbols: list[str] = []

    async def run_scan(session, *, trigger_type, priority_symbols=None):
        observed_priority_symbols.extend(priority_symbols or [])
        cycle = ScanCycle(
            id=9,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status=ScanStatus.COMPLETE,
            symbols_scanned=0,
            candidates_found=0,
            signals_qualified=0,
            trigger_type=trigger_type,
            progress_pct=100,
        )
        session.add(cycle)
        return cycle

    session = bind_session_state(FakeSession(), service)
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    service.session_factory = lambda: DummyAsyncContext(session)

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    result = await service.run_cycle(reason="interval")

    assert result is True
    assert observed_priority_symbols == ["DRIFTUSDT"]
    assert "DRIFTUSDT" not in service.drift_rows


@pytest.mark.asyncio
async def test_auto_mode_rebalances_to_stronger_pending_signal() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [
        make_order(order_id=1, symbol="OLD120USDT", status=OrderStatus.ORDER_PLACED),
        make_order(order_id=2, symbol="OLD100USDT", status=OrderStatus.ORDER_PLACED),
        make_order(order_id=3, symbol="OLD070USDT", status=OrderStatus.ORDER_PLACED),
    ]
    service.active_local_orders = list(service.active_auto_orders)
    service.pending_scores = {
        1: (120, 70),
        2: (100, 65),
        3: (70, 55),
    }
    actionable_signals = [
        make_signal(signal_id=10, symbol="NEW100USDT", direction=SignalDirection.LONG, cycle_id=9),
        make_signal(signal_id=11, symbol="NEW090USDT", direction=SignalDirection.LONG, cycle_id=9),
        make_signal(signal_id=12, symbol="NEW085USDT", direction=SignalDirection.LONG, cycle_id=9),
    ]
    actionable_signals[0].final_score = 100
    actionable_signals[0].confirmation_score = 60
    actionable_signals[1].final_score = 90
    actionable_signals[1].confirmation_score = 60
    actionable_signals[2].final_score = 85
    actionable_signals[2].confirmation_score = 60
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == [(3, "setup_state_changed")]
    assert [call["signal_id"] for call in order_manager.approve_calls] == [10]


@pytest.mark.asyncio
async def test_auto_mode_in_position_orders_count_toward_slot_cap_and_are_not_replaced() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [
        make_order(order_id=1, symbol="LOCK1USDT", status=OrderStatus.IN_POSITION),
        make_order(order_id=2, symbol="LOCK2USDT", status=OrderStatus.IN_POSITION),
        make_order(order_id=3, symbol="LOCK3USDT", status=OrderStatus.IN_POSITION),
    ]
    service.active_local_orders = list(service.active_auto_orders)
    actionable_signals = [make_signal(signal_id=10, symbol="HIGHUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].final_score = 150
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.approve_calls == []
    assert order_manager.close_calls == []
    assert order_manager.cancel_calls == []


@pytest.mark.asyncio
async def test_auto_mode_allows_same_symbol_refresh_when_new_signal_scores_higher() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [make_order(order_id=1, symbol="BTCUSDT", status=OrderStatus.ORDER_PLACED)]
    service.active_local_orders = list(service.active_auto_orders)
    service.pending_scores = {1: (70, 50)}
    actionable_signals = [make_signal(signal_id=10, symbol="BTCUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].final_score = 100
    actionable_signals[0].confirmation_score = 60
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == [(1, "setup_state_changed")]
    assert [call["signal_id"] for call in order_manager.approve_calls] == [10]


@pytest.mark.asyncio
async def test_auto_mode_manual_pending_order_blocks_same_symbol_open() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = []
    service.active_local_orders = [make_order(order_id=1, symbol="BTCUSDT", status=OrderStatus.ORDER_PLACED, approved_by="LEGACY_MODE")]
    actionable_signals = [make_signal(signal_id=10, symbol="BTCUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].final_score = 100
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.approve_calls == []
    assert order_manager.cancel_calls == []


@pytest.mark.asyncio
async def test_auto_mode_keeps_existing_pending_order_on_exact_score_tie() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [make_order(order_id=1, symbol="BTCUSDT", status=OrderStatus.ORDER_PLACED)]
    service.active_local_orders = list(service.active_auto_orders)
    service.pending_scores = {1: (100, 60)}
    actionable_signals = [make_signal(signal_id=10, symbol="BTCUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].final_score = 100
    actionable_signals[0].confirmation_score = 60
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == []
    assert order_manager.approve_calls == []


@pytest.mark.asyncio
async def test_auto_mode_uses_rank_value_before_final_score_when_replacing_pending_orders() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [make_order(order_id=1, symbol="BTCUSDT", status=OrderStatus.ORDER_PLACED)]
    service.active_local_orders = list(service.active_auto_orders)
    service.pending_scores = {1: (95, 60)}
    service.pending_rank_values = {1: 95.0}
    actionable_signals = [make_signal(signal_id=10, symbol="BTCUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    actionable_signals[0].final_score = 96
    actionable_signals[0].confirmation_score = 60
    actionable_signals[0].rank_value = Decimal("94.0")
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert order_manager.cancel_calls == []
    assert order_manager.approve_calls == []


@pytest.mark.asyncio
async def test_auto_mode_uses_lower_ranked_actionable_signal_when_higher_ranked_symbols_are_blocked() -> None:
    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [
        make_order(
            order_id=1,
            symbol="LOCKEDUSDT",
            status=OrderStatus.IN_POSITION,
            direction=SignalDirection.SHORT,
        )
    ]
    service.active_local_orders = service.active_auto_orders + [make_order(order_id=2, symbol="LEGACYUSDT", status=OrderStatus.ORDER_PLACED, approved_by="LEGACY_MODE")]
    actionable_signals = [
        make_signal(signal_id=10, symbol="LEGACYUSDT", direction=SignalDirection.LONG, cycle_id=9),
        make_signal(signal_id=11, symbol="LOCKEDUSDT", direction=SignalDirection.LONG, cycle_id=9),
        make_signal(signal_id=12, symbol="FALLBACKUSDT", direction=SignalDirection.LONG, cycle_id=9),
    ]
    actionable_signals[0].extra_context = {"strategy_key": "aqrr_binance_usdm"}
    actionable_signals[1].extra_context = {"strategy_key": "aqrr_binance_usdm"}
    actionable_signals[2].extra_context = {"strategy_key": "aqrr_binance_usdm"}
    actionable_signals[0].final_score = 120
    actionable_signals[1].final_score = 110
    actionable_signals[2].final_score = 90
    session = bind_session_state(FakeSession(), service, actionable_signals)

    await service._rebalance_pending_orders(
        session,
        scan_cycle=ScanCycle(id=9, trigger_type=TriggerType.AUTO_MODE),
        settings_map=AUTO_MODE_SETTINGS,
        actionable_signals=actionable_signals,
    )

    assert [call["signal_id"] for call in order_manager.approve_calls] == [12]


@pytest.mark.asyncio
async def test_auto_mode_run_cycle_syncs_orders_before_scanning(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    order_manager = StubOrderManager()
    order_manager.sync_updates = {1: OrderStatus.IN_POSITION}
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [make_order(order_id=1, symbol="SYNCUSDT", status=OrderStatus.ORDER_PLACED)]
    service.active_local_orders = list(service.active_auto_orders)

    scan_observed_statuses: list[OrderStatus] = []

    async def run_scan(session, *, trigger_type, priority_symbols=None):
        scan_observed_statuses.append(service.active_auto_orders[0].status)
        cycle = ScanCycle(
            id=9,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status=ScanStatus.COMPLETE,
            symbols_scanned=0,
            candidates_found=0,
            signals_qualified=0,
            trigger_type=trigger_type,
            progress_pct=100,
        )
        session.add(cycle)
        return cycle

    session = bind_session_state(FakeSession(), service)
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    service.session_factory = lambda: DummyAsyncContext(session)

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    result = await service.run_cycle(reason="interval")

    assert result is True
    assert order_manager.sync_calls == [1]
    assert scan_observed_statuses == [OrderStatus.IN_POSITION]


@pytest.mark.asyncio
async def test_auto_mode_queue_cycle_runs_immediately(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    observed_scan_calls: list[tuple[TriggerType, list[str]]] = []

    async def run_scan(session, *, trigger_type, priority_symbols=None):
        observed_scan_calls.append((trigger_type, list(priority_symbols or [])))
        cycle = ScanCycle(
            id=9,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status=ScanStatus.COMPLETE,
            symbols_scanned=0,
            candidates_found=0,
            signals_qualified=0,
            trigger_type=trigger_type,
            progress_pct=100,
        )
        session.add(cycle)
        return cycle

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    session = bind_session_state(FakeSession(), service)
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    service.session_factory = lambda: DummyAsyncContext(session)

    queued = await service.queue_cycle(reason="enabled")
    task = service._queued_task

    assert queued is True
    assert task is not None

    result = await task

    assert result is True
    assert observed_scan_calls == [(TriggerType.AUTO_MODE, [])]
    assert service.broadcast_calls == ["cycle_started", "cycle_finished"]
    assert service.running is False


@pytest.mark.asyncio
async def test_auto_mode_cycle_complete_audit_marks_no_qualified_signal_cycles(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    async def run_scan(session, *, trigger_type, priority_symbols=None):
        cycle = ScanCycle(
            id=9,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status=ScanStatus.COMPLETE,
            symbols_scanned=0,
            candidates_found=0,
            signals_qualified=0,
            trigger_type=trigger_type,
            progress_pct=100,
        )
        session.add(cycle)
        return cycle

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    session = bind_session_state(FakeSession(), service)
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    service.session_factory = lambda: DummyAsyncContext(session)

    result = await service.run_cycle(reason="interval")

    assert result is True
    cycle_complete = audit_entries(session, event_type="AUTO_MODE_CYCLE_COMPLETE")[0]
    assert cycle_complete.details["candidate_count"] == 0
    assert cycle_complete.details["qualified_count"] == 0
    assert cycle_complete.details["opened_order_count"] == 0
    assert cycle_complete.details["remaining_slot_count"] == 3
    assert cycle_complete.details["skipped_because_no_qualified_signals"] is True


@pytest.mark.asyncio
async def test_auto_mode_cycle_complete_audit_distinguishes_full_slots_from_no_signal(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    async def run_scan(session, *, trigger_type, priority_symbols=None):
        cycle = ScanCycle(
            id=9,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status=ScanStatus.COMPLETE,
            symbols_scanned=0,
            candidates_found=1,
            signals_qualified=1,
            trigger_type=trigger_type,
            progress_pct=100,
        )
        session.add(cycle)
        return cycle

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    locked_orders = [
        make_order(order_id=1, symbol="LOCK1USDT", status=OrderStatus.IN_POSITION),
        make_order(order_id=2, symbol="LOCK2USDT", status=OrderStatus.IN_POSITION),
        make_order(order_id=3, symbol="LOCK3USDT", status=OrderStatus.IN_POSITION),
    ]
    service.active_auto_orders = list(locked_orders)
    service.active_local_orders = list(locked_orders)
    service.actionable_signals = [make_signal(signal_id=10, symbol="OPENUSDT", direction=SignalDirection.LONG, cycle_id=9)]
    session = bind_session_state(FakeSession(), service, service.actionable_signals)
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    service.session_factory = lambda: DummyAsyncContext(session)

    result = await service.run_cycle(reason="interval")

    assert result is True
    cycle_complete = audit_entries(session, event_type="AUTO_MODE_CYCLE_COMPLETE")[0]
    assert cycle_complete.details["candidate_count"] == 1
    assert cycle_complete.details["qualified_count"] == 1
    assert cycle_complete.details["active_slot_count"] == 3
    assert cycle_complete.details["remaining_slot_count"] == 0
    assert cycle_complete.details["opened_order_count"] == 0
    assert cycle_complete.details["skipped_because_no_qualified_signals"] is False


@pytest.mark.asyncio
async def test_auto_mode_run_cycle_preserves_drift_candidate_until_rebalance(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    observed_priority_symbols: list[str] = []

    async def run_scan(session, *, trigger_type, priority_symbols=None):
        observed_priority_symbols.extend(priority_symbols or [])
        cycle = ScanCycle(
            id=9,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            status=ScanStatus.COMPLETE,
            symbols_scanned=0,
            candidates_found=0,
            signals_qualified=0,
            trigger_type=trigger_type,
            progress_pct=100,
        )
        session.add(cycle)
        return cycle

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway(mark_prices_payload={"DRIFTUSDT": {"markPrice": "100"}}))
    pending_order = make_order(order_id=1, symbol="DRIFTUSDT", status=OrderStatus.ORDER_PLACED)
    pending_order.entry_price = Decimal("97")
    service.active_auto_orders = [pending_order]
    service.active_local_orders = [pending_order]
    session = bind_session_state(FakeSession(), service)
    session.scan_result_rows = [
        SimpleNamespace(
            symbol="DRIFTUSDT",
            direction=SignalDirection.LONG,
            filter_reasons=["entry_too_far_from_mark"],
        )
    ]
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    service.session_factory = lambda: DummyAsyncContext(session)

    result = await service.run_cycle(reason="interval")

    assert result is True
    assert observed_priority_symbols == []
    assert order_manager.cancel_calls == []
    assert service.drift_rows == {}
    assert service.broadcast_calls == ["cycle_started", "cycle_finished"]


@pytest.mark.asyncio
async def test_get_auto_mode_status_delegates_to_service() -> None:
    expected = AutoModeStatusRead(
        enabled=True,
        paused=False,
        running=False,
        signal_schedule="15m_closed_candle",
        kill_switch_active=False,
        kill_switch_reason=None,
        active_order_count=1,
        active_risk_usdt=Decimal("10"),
        portfolio_risk_budget_usdt=Decimal("80"),
        per_slot_risk_budget_usdt=Decimal("26.67"),
        next_cycle_at=datetime.now(timezone.utc),
    )

    async def fake_get_status(_session, *, next_cycle_at):
        assert next_cycle_at == expected.next_cycle_at
        return expected

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                scheduler_service=SimpleNamespace(auto_mode_next_run_at=lambda: expected.next_cycle_at),
                auto_mode_service=SimpleNamespace(get_status=fake_get_status),
            )
        )
    )

    response = await get_auto_mode_status(request, FakeSession())

    assert response == expected


@pytest.mark.asyncio
async def test_auto_mode_get_status_returns_disabled_payload_when_mode_is_off(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "false",
            "risk_per_trade_pct": "1.0",
            "max_portfolio_risk_pct": "3.0",
            "max_leverage": "10",
        }

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)

    service = AutoModeTestService(order_manager=StubOrderManager(), gateway=StubGateway())
    async def fake_latest_auto_cycle(_session):
        return None

    service._latest_auto_mode_cycle = fake_latest_auto_cycle
    response = await service.get_status(FakeSession(), next_cycle_at=None)

    assert response.enabled is False
    assert response.signal_schedule == "15m_closed_candle"
    assert response.kill_switch_active is False
    assert response.running is False
    assert response.active_order_count == 0
    assert response.next_cycle_at is None


@pytest.mark.asyncio
async def test_auto_mode_get_status_reports_remaining_slot_budget() -> None:
    order_manager = StubOrderManager()
    order_manager.available_balance = Decimal("90")
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    existing_order = make_order(order_id=1, symbol="OPENUSDT", status=OrderStatus.IN_POSITION, risk_usdt_at_stop=Decimal("1"))
    service.active_auto_orders = [existing_order]
    service.active_local_orders = [existing_order]

    async def fake_latest_auto_cycle(_session):
        return None

    service._latest_auto_mode_cycle = fake_latest_auto_cycle
    response = await service.get_status(bind_session_state(FakeSession(), service), next_cycle_at=None)

    assert response.portfolio_risk_budget_usdt == Decimal("5.40")
    assert response.per_slot_risk_budget_usdt == Decimal("1.80")
    assert response.active_order_count == 1
    assert response.kill_switch_active is False


@pytest.mark.asyncio
async def test_update_auto_mode_returns_current_status_when_no_changes_are_sent(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {"auto_mode_enabled": "false"}

    monkeypatch.setattr(auto_mode_router, "get_settings_map", fake_get_settings_map)

    expected = AutoModeStatusRead(
        enabled=False,
        paused=False,
        running=False,
        signal_schedule="15m_closed_candle",
        kill_switch_active=False,
        kill_switch_reason=None,
        active_order_count=0,
        active_risk_usdt=Decimal("0"),
        portfolio_risk_budget_usdt=Decimal("0"),
        per_slot_risk_budget_usdt=Decimal("0"),
        next_cycle_at=None,
    )

    async def fake_get_status(_session, *, next_cycle_at):
        assert next_cycle_at is None
        return expected

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                scheduler_service=SimpleNamespace(reload=lambda **_kwargs: None, auto_mode_next_run_at=lambda: None),
                auto_mode_service=SimpleNamespace(broadcast_state=lambda **_kwargs: None, get_status=fake_get_status),
            )
        )
    )

    response = await update_auto_mode(AutoModeUpdateRequest(), request, FakeSession())

    assert response == expected


@pytest.mark.asyncio
async def test_auto_mode_shutdown_cancels_orders_with_stop_reason(monkeypatch) -> None:
    async def fake_record_audit(session, *, event_type: str, **_kwargs):
        session.audits.append(event_type)
        return None

    monkeypatch.setattr("app.services.auto_mode.record_audit", fake_record_audit)

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.active_auto_orders = [
        make_order(order_id=1, symbol="PENDINGUSDT", status=OrderStatus.ORDER_PLACED),
        make_order(order_id=2, symbol="OPENUSDT", status=OrderStatus.IN_POSITION),
    ]
    service.active_local_orders = list(service.active_auto_orders)
    session = bind_session_state(FakeSession(), service)
    service.session_factory = lambda: DummyAsyncContext(session)

    await service.shutdown(broadcast_reason="stopped")

    assert order_manager.cancel_calls == [(1, "viability_lost")]
    assert order_manager.close_calls == []
    assert "AUTO_MODE_STOPPED" in session.audits


@pytest.mark.asyncio
async def test_auto_mode_run_cycle_cancellation_records_cancel_audit(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
            **AUTO_MODE_RUNTIME_SETTINGS,
        }

    async def fake_record_audit(session, *, event_type: str, **_kwargs):
        session.audits.append(event_type)
        return None

    async def run_scan(_session, *, trigger_type, priority_symbols=None):
        assert trigger_type == TriggerType.AUTO_MODE
        assert priority_symbols == []
        await asyncio.sleep(10)
        return ScanCycle(id=9, trigger_type=trigger_type)

    monkeypatch.setattr("app.services.auto_mode.get_settings_map", fake_get_settings_map)
    monkeypatch.setattr("app.services.auto_mode.record_audit", fake_record_audit)

    order_manager = StubOrderManager()
    service = AutoModeTestService(order_manager=order_manager, gateway=StubGateway())
    service.scanner_service = SimpleNamespace(run_scan=run_scan)
    session = bind_session_state(FakeSession(), service)
    service.session_factory = lambda: DummyAsyncContext(session)

    task = asyncio.create_task(service.run_cycle(reason="interval"))
    await asyncio.sleep(0)

    cancelled = await service.stop()
    result = await task

    assert cancelled is True
    assert result is False
    assert "AUTO_MODE_CYCLE_CANCELLED" in session.audits


class DummyAsyncContext:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_scheduler_start_prefers_auto_mode_job_and_queues_immediate_cycle(monkeypatch) -> None:
    async def fake_get_settings_map(_session):
        return {
            "auto_mode_enabled": "true",
        }

    class StubAutoModeRunner:
        def __init__(self) -> None:
            self.queue_calls: list[str] = []

        async def queue_cycle(self, *, reason: str) -> bool:
            self.queue_calls.append(reason)
            return True

        async def run_cycle(self, *, reason: str) -> bool:
            self.queue_calls.append(reason)
            return True

    monkeypatch.setattr(scheduler_module, "get_settings_map", fake_get_settings_map)

    auto_mode_service = StubAutoModeRunner()
    scheduler = SchedulerService(
        auto_mode_service=auto_mode_service,
        session_factory=lambda: DummyAsyncContext(FakeSession()),
    )
    await scheduler.start()
    try:
        assert scheduler.scheduler.get_job("auto-mode-scan") is not None
        assert scheduler.scheduler.get_job("daily-scan") is None
        assert auto_mode_service.queue_calls == ["enabled"]
    finally:
        await scheduler.stop()
