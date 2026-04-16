from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.models.order  # noqa: F401
import app.models.scan_cycle  # noqa: F401
import app.models.scan_symbol_result  # noqa: F401
import app.services.order_manager as order_manager_module
from app.models.audit_log import AuditLog
from app.models.enums import OrderStatus, SignalDirection, SignalStatus
from app.models.order import Order
from app.models.signal import Signal
from app.services.binance_gateway import BinanceAPIError, LeverageBracket, SymbolFilters
from app.services.order_manager import AccountSnapshot, EntryOrderState, OrderApprovalExchangeError, OrderManager
from app.services.ws_manager import WebSocketManager


DEFAULT_SETTINGS_MAP = {
    "risk_per_trade_pct": "2.0",
    "max_portfolio_risk_pct": "6.0",
    "max_leverage": "10",
    "deployable_equity_pct": "90",
    "max_book_spread_bps": "12",
    "min_24h_quote_volume_usdt": "25000000",
    "kill_switch_consecutive_stop_losses": "2",
    "kill_switch_daily_drawdown_pct": "4.0",
}


class FakeSession:
    def __init__(self, signal: Signal, *, order: Order | None = None):
        self.signal = signal
        self.order = order
        self.added: list[object] = []
        self.committed = False
        self.refreshed: list[object] = []
        self._ids: dict[type, int] = {}

    def add(self, obj: object) -> None:
        obj_type = type(obj)
        next_id = self._ids.get(obj_type, 0) + 1
        self._ids[obj_type] = next_id
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            setattr(obj, "id", next_id)
        self.added.append(obj)

    async def get(self, model, obj_id: int):
        if model is Signal and self.signal.id == obj_id:
            return self.signal
        if model is Order and self.order is not None and self.order.id == obj_id:
            return self.order
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: object) -> None:
        self.refreshed.append(obj)

    async def execute(self, _query):
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None),
        )


class FakeGateway:
    def __init__(
        self,
        *,
        filters: SymbolFilters,
        place_order_results: list[dict | Exception] | None = None,
        place_algo_order_results: list[dict | Exception] | None = None,
        account_info: dict | None = None,
        mark_price: str = "100.0",
        leverage_brackets: list[LeverageBracket] | None = None,
        query_order_results: dict[str, dict | Exception] | None = None,
        query_algo_order_results: dict[str, dict | Exception] | None = None,
        account_trade_results: dict[str, list[dict]] | None = None,
        cancel_order_results: dict[str, dict | Exception] | None = None,
        cancel_algo_order_results: dict[str, dict | Exception] | None = None,
        position_mode_value: bool | Exception = False,
        positions_payload: list[dict] | None = None,
    ) -> None:
        self.filters = filters
        self.place_order_results = list(place_order_results or [])
        self.place_algo_order_results = list(place_algo_order_results or [])
        self.account_info_payload = account_info or {
            "totalWalletBalance": "10",
            "availableBalance": "10",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        }
        self.mark_price_value = mark_price
        self.query_order_results = dict(query_order_results or {})
        self.query_algo_order_results = dict(query_algo_order_results or {})
        self.account_trade_results = {key: list(value) for key, value in (account_trade_results or {}).items()}
        self.cancel_order_results = dict(cancel_order_results or {})
        self.cancel_algo_order_results = dict(cancel_algo_order_results or {})
        self.position_mode_value = position_mode_value
        self.positions_payload = list(positions_payload or [])
        self.leverage_brackets_payload = leverage_brackets or [
            LeverageBracket(
                bracket=1,
                initial_leverage=10,
                notional_cap=Decimal("100000"),
                notional_floor=Decimal("0"),
                maint_margin_ratio=Decimal("0.005"),
                cum=Decimal("0"),
            )
        ]
        self.place_order_calls: list[dict] = []
        self.place_algo_order_calls: list[dict] = []
        self.query_order_calls: list[tuple[str, str]] = []
        self.query_algo_order_calls: list[str] = []
        self.account_trade_calls: list[dict[str, object]] = []
        self.cancel_order_calls: list[tuple[str, str]] = []
        self.cancel_algo_order_calls: list[str] = []
        self.position_mode_probe_calls = 0
        self.position_mode_calls: list[bool] = []
        self.margin_type_calls: list[tuple[str, str]] = []
        self.leverage_calls: list[tuple[str, int]] = []
        self.mark_price_calls: list[str] = []

    async def account_info(self, _credentials) -> dict:
        return self.account_info_payload

    async def positions(self, _credentials) -> list[dict]:
        return list(self.positions_payload)

    async def exchange_info(self) -> dict:
        return {"symbols": [{"symbol": self.filters.symbol}]}

    async def leverage_brackets(self, _credentials, symbol: str | None = None) -> dict[str, list[LeverageBracket]]:
        target = symbol or self.filters.symbol
        return {target: self.leverage_brackets_payload}

    def parse_symbol_filters(self, _exchange_info: dict) -> dict[str, SymbolFilters]:
        return {self.filters.symbol: self.filters}

    async def mark_price(self, symbol: str) -> dict:
        self.mark_price_calls.append(symbol)
        return {"markPrice": self.mark_price_value, "lastFundingRate": "0.0"}

    async def get_position_mode(self, _credentials) -> bool:
        self.position_mode_probe_calls += 1
        if isinstance(self.position_mode_value, Exception):
            raise self.position_mode_value
        return self.position_mode_value

    async def change_position_mode(self, _credentials, dual_side: bool = False) -> dict:
        self.position_mode_calls.append(dual_side)
        return {"msg": "ok"}

    async def change_margin_type(self, _credentials, symbol: str, margin_type: str) -> dict:
        self.margin_type_calls.append((symbol, margin_type))
        return {"msg": "ok"}

    async def change_leverage(self, _credentials, symbol: str, leverage: int) -> dict:
        self.leverage_calls.append((symbol, leverage))
        return {"leverage": leverage}

    async def place_order(self, _credentials, params: dict) -> dict:
        self.place_order_calls.append(params)
        next_result = self.place_order_results.pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return next_result

    async def place_algo_order(self, _credentials, params: dict) -> dict:
        self.place_algo_order_calls.append(params)
        next_result = self.place_algo_order_results.pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return next_result

    async def query_order(self, _credentials, symbol: str, order_id: str | None, *, orig_client_order_id: str | None = None) -> dict:
        lookup_key = str(orig_client_order_id or order_id)
        self.query_order_calls.append((symbol, lookup_key))
        result = self.query_order_results.get(lookup_key, {"status": "NEW"})
        if isinstance(result, Exception):
            raise result
        return result

    async def query_algo_order(self, _credentials, algo_id: str | None, *, client_algo_id: str | None = None) -> dict:
        lookup_key = str(client_algo_id or algo_id)
        self.query_algo_order_calls.append(lookup_key)
        result = self.query_algo_order_results.get(lookup_key, {"algoStatus": "NEW", "actualOrderId": ""})
        if isinstance(result, Exception):
            raise result
        return result

    async def account_trades(
        self,
        _credentials,
        symbol: str,
        *,
        order_id: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        from_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        self.account_trade_calls.append(
            {
                "symbol": symbol,
                "order_id": order_id,
                "start_time": start_time,
                "end_time": end_time,
                "from_id": from_id,
                "limit": limit,
            }
        )
        if order_id is not None:
            return list(self.account_trade_results.get(str(order_id), []))
        return list(self.account_trade_results.get(symbol, []))

    async def cancel_order(self, _credentials, symbol: str, order_id: str) -> dict:
        self.cancel_order_calls.append((symbol, order_id))
        result = self.cancel_order_results.get(order_id, {"status": "CANCELED"})
        if isinstance(result, Exception):
            raise result
        return result

    async def cancel_algo_order(self, _credentials, algo_id: str) -> dict:
        self.cancel_algo_order_calls.append(algo_id)
        result = self.cancel_algo_order_results.get(algo_id, {"code": "200"})
        if isinstance(result, Exception):
            raise result
        return result


class RecalculationGateway(FakeGateway):
    def __init__(
        self,
        *,
        filters: SymbolFilters,
        first_error: BinanceAPIError,
        account_info_after_recalc: dict,
        retry_result: dict | Exception,
        mark_price: str = "100.07",
        leverage_brackets: list[LeverageBracket] | None = None,
    ) -> None:
        super().__init__(
            filters=filters,
            place_order_results=[],
            place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
            mark_price=mark_price,
            leverage_brackets=leverage_brackets,
        )
        self.first_error = first_error
        self.account_info_after_recalc = account_info_after_recalc
        self.retry_result = retry_result
        self._attempt = 0

    async def place_order(self, _credentials, params: dict) -> dict:
        self.place_order_calls.append(params)
        if self._attempt == 0:
            self._attempt += 1
            self.account_info_payload = self.account_info_after_recalc
            raise self.first_error
        if isinstance(self.retry_result, Exception):
            raise self.retry_result
        return self.retry_result


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, **payload) -> None:
        self.messages.append(payload)


class CapturingWebSocketManager(WebSocketManager):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


class ApprovalOrderManager(OrderManager):
    def __init__(
        self,
        gateway: FakeGateway,
        ws_manager: CapturingWebSocketManager,
        notifier: FakeNotifier,
        *,
        latest_completed_scan_id: int | None = 7,
        active_orders: list[Order] | None = None,
    ) -> None:
        super().__init__(gateway, ws_manager, notifier)
        self.latest_completed_scan_id = latest_completed_scan_id
        self._active_orders = list(active_orders or [])

    async def get_credentials(self, _session):
        return SimpleNamespace(api_key="key", private_key_pem="private")

    async def _latest_completed_scan_id(self, _session) -> int | None:
        return self.latest_completed_scan_id

    async def active_entry_orders(self, _session) -> list[Order]:
        return list(self._active_orders)


@pytest.fixture(autouse=True)
def patch_settings_map(monkeypatch):
    async def fake_get_settings_map(_session) -> dict[str, str]:
        return dict(DEFAULT_SETTINGS_MAP)

    monkeypatch.setattr(order_manager_module, "get_settings_map", fake_get_settings_map)


def make_filters(
    symbol: str = "BCHUSDT",
    *,
    tick_size: str = "0.1",
    step_size: str = "0.001",
    min_notional: str = "5",
    max_qty: str | None = None,
    market_step_size: str | None = None,
    market_min_qty: str | None = None,
    market_max_qty: str | None = None,
    percent_price_multiplier_up: str | None = None,
    percent_price_multiplier_down: str | None = None,
) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        tick_size=Decimal(tick_size),
        step_size=Decimal(step_size),
        min_qty=Decimal("0.001"),
        min_notional=Decimal(min_notional),
        max_qty=None if max_qty is None else Decimal(max_qty),
        market_step_size=None if market_step_size is None else Decimal(market_step_size),
        market_min_qty=None if market_min_qty is None else Decimal(market_min_qty),
        market_max_qty=None if market_max_qty is None else Decimal(market_max_qty),
        percent_price_multiplier_up=None if percent_price_multiplier_up is None else Decimal(percent_price_multiplier_up),
        percent_price_multiplier_down=None if percent_price_multiplier_down is None else Decimal(percent_price_multiplier_down),
    )


def make_signal(
    *,
    direction: SignalDirection,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    scan_cycle_id: int = 7,
) -> Signal:
    return Signal(
        id=1,
        scan_cycle_id=scan_cycle_id,
        symbol="BCHUSDT",
        direction=direction,
        timeframe="4h",
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        rr_ratio=Decimal("3.00"),
        confirmation_score=72,
        final_score=88,
        score_breakdown={"trend": 72},
        reason_text="Qualified setup",
        swing_origin=Decimal("90"),
        swing_terminus=Decimal("110"),
        fib_0786_level=entry_price,
        current_price_at_signal=entry_price,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        status=SignalStatus.QUALIFIED,
        extra_context={},
    )

def make_snapshot(balance: str) -> AccountSnapshot:
    return AccountSnapshot.from_available_balance(Decimal(balance), reserve_fraction=OrderManager.BALANCE_RESERVE_FRACTION)


def assert_no_remote_submit_calls(gateway: FakeGateway) -> None:
    assert gateway.position_mode_calls == []
    assert gateway.margin_type_calls == []
    assert gateway.leverage_calls == []
    assert gateway.place_order_calls == []
    assert gateway.place_algo_order_calls == []


def make_partial_order(*, signal: Signal, status: OrderStatus = OrderStatus.IN_POSITION) -> Order:
    return Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("114.7"),
        partial_tp_enabled=True,
        take_profit_1=Decimal("107.4"),
        take_profit_2=Decimal("114.7"),
        tp_quantity_1=Decimal("0.049"),
        tp_quantity_2=Decimal("0.050"),
        quantity=Decimal("0.099"),
        remaining_quantity=Decimal("0.099"),
        position_margin=Decimal("3.30231"),
        notional_value=Decimal("9.9"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="202",
        tp_order_1_id="201",
        tp_order_2_id="202",
        sl_order_id="203",
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )


def make_entry_fill_order(
    *,
    signal: Signal,
    status: OrderStatus,
    entry_style: str | None = None,
    expires_at: datetime | None = None,
) -> Order:
    return Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        entry_style=entry_style,
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=status,
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=24)),
        approved_by="AUTO_MODE",
    )


def make_stale(order: Order, *, minutes: int = 10) -> Order:
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    order.created_at = stale_at
    order.updated_at = stale_at
    return order


def make_active_order(*, symbol: str, status: OrderStatus = OrderStatus.ORDER_PLACED) -> Order:
    return Order(
        id=99,
        signal_id=1,
        symbol=symbol,
        direction=SignalDirection.LONG,
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
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )


def test_build_shared_entry_slot_budget_uses_deployable_equity_when_no_orders_are_active() -> None:
    manager = OrderManager(gateway=None, ws_manager=WebSocketManager(), notifier=FakeNotifier())

    budget = manager.build_shared_entry_slot_budget(available_balance=Decimal("90"), active_entry_order_count=0)

    assert budget.slot_cap == 3
    assert budget.remaining_entry_slots == 3
    assert budget.deployable_equity == Decimal("81.00")
    assert budget.remaining_deployable_equity == Decimal("81.00")
    assert budget.per_slot_budget == Decimal("27.00")


def test_build_shared_entry_slot_budget_uses_remaining_deployable_equity_when_one_order_is_active() -> None:
    manager = OrderManager(gateway=None, ws_manager=WebSocketManager(), notifier=FakeNotifier())

    budget = manager.build_shared_entry_slot_budget(
        available_balance=Decimal("90"),
        committed_initial_margin=Decimal("9"),
        active_entry_orders=[make_active_order(symbol="OPENUSDT")],
    )

    assert budget.remaining_entry_slots == 2
    assert budget.remaining_deployable_equity == Decimal("72.00")
    assert budget.per_slot_budget == Decimal("36.00")


def test_build_shared_entry_slot_budget_returns_zero_when_no_slots_remain() -> None:
    manager = OrderManager(gateway=None, ws_manager=WebSocketManager(), notifier=FakeNotifier())

    budget = manager.build_shared_entry_slot_budget(
        available_balance=Decimal("90"),
        active_entry_orders=[
            make_active_order(symbol="OPEN1USDT"),
            make_active_order(symbol="OPEN2USDT", status=OrderStatus.IN_POSITION),
            make_active_order(symbol="OPEN3USDT"),
        ],
    )

    assert budget.remaining_entry_slots == 0
    assert budget.per_slot_budget == Decimal("0")


def test_build_execution_plan_uses_mark_price_and_resizes_short_order() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters(min_notional="20"), place_order_results=[]),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )

    execution = manager.build_execution_plan(
        symbol="BCHUSDT",
        account_snapshot=make_snapshot("16.72160381"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(min_notional="20"),
        direction=SignalDirection.SHORT,
        entry_price=Decimal("475.2385"),
        stop_loss=Decimal("481.44"),
        take_profit=Decimal("456.634"),
        mark_price=Decimal("482.95"),
        leverage_brackets=[
            LeverageBracket(
                bracket=1,
                initial_leverage=75,
                notional_cap=Decimal("10000"),
                notional_floor=Decimal("0"),
                maint_margin_ratio=Decimal("0.005"),
                cum=Decimal("0"),
            )
        ],
    )

    preview = execution["order_preview"]

    assert execution["entry_price"] == Decimal("475.3")
    assert execution["stop_loss"] == Decimal("481.5")
    assert execution["take_profit"] == Decimal("456.6")
    assert preview["status"] == "resized_to_budget"
    assert preview["can_place"] is True
    assert preview["auto_resized"] is True
    assert preview["recommended_leverage"] == 5
    assert preview["requested_quantity"] == "0.103"
    assert preview["final_quantity"] == "0.051"
    assert preview["max_affordable_quantity"] == "0.051"
    assert preview["required_initial_margin"] == "4.92609"
    assert preview["available_balance"] == "16.72160381"
    assert preview["risk_budget_usdt"] == "5.016481143"


def test_build_execution_plan_returns_too_small_for_exchange_when_budget_cannot_reach_min_notional() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters(min_notional="20"), place_order_results=[]),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )

    execution = manager.build_execution_plan(
        symbol="BCHUSDT",
        account_snapshot=make_snapshot("1"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(min_notional="20"),
        direction=SignalDirection.SHORT,
        entry_price=Decimal("475.2385"),
        stop_loss=Decimal("481.44"),
        take_profit=Decimal("456.634"),
        mark_price=Decimal("482.95"),
        leverage_brackets=[
            LeverageBracket(
                bracket=1,
                initial_leverage=75,
                notional_cap=Decimal("10000"),
                notional_floor=Decimal("0"),
                maint_margin_ratio=Decimal("0.005"),
                cum=Decimal("0"),
            )
        ],
    )

    preview = execution["order_preview"]

    assert preview["status"] == "too_small_for_exchange"
    assert preview["can_place"] is False
    assert "below Binance minimum notional" in (preview["reason"] or "")


def test_build_execution_plan_rejects_when_percent_price_filter_is_violated() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(
            filters=make_filters(
                percent_price_multiplier_up="1.01",
                percent_price_multiplier_down="0.99",
            ),
            place_order_results=[],
        ),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )
    execution = manager.build_execution_plan(
        symbol="BCHUSDT",
        account_snapshot=make_snapshot("100"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(
            percent_price_multiplier_up="1.01",
            percent_price_multiplier_down="0.99",
        ),
        direction=SignalDirection.LONG,
        entry_price=Decimal("95.0"),
        stop_loss=Decimal("90.0"),
        take_profit=Decimal("110.0"),
        mark_price=Decimal("100.0"),
        leverage_brackets=[],
    )

    assert execution["error"] == "percent_price_filter_failed"
    assert "PERCENT_PRICE" in manager._preview_error_message("BCHUSDT", execution)


def test_build_preview_enforces_market_lot_size_min_qty() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(
            filters=make_filters(market_min_qty="0.05", market_step_size="0.001"),
            place_order_results=[],
        ),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )
    preview = manager.build_preview(
        balance=Decimal("0.5"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(market_min_qty="0.05", market_step_size="0.001"),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
    )

    assert preview["status"] == "too_small_for_exchange"
    assert preview["can_place"] is False
    assert "0.05" in str(preview["reason"])


def test_build_preview_enforces_lot_size_max_qty() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(
            filters=make_filters(max_qty="0.05"),
            place_order_results=[],
        ),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )
    preview = manager.build_preview(
        balance=Decimal("100"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(max_qty="0.05"),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
    )

    assert preview["status"] == "too_large_for_exchange"
    assert preview["can_place"] is False


def test_build_preview_enforces_market_lot_size_max_qty() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(
            filters=make_filters(market_max_qty="0.05"),
            place_order_results=[],
        ),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )
    preview = manager.build_preview(
        balance=Decimal("100"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(market_max_qty="0.05"),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
    )

    assert preview["status"] == "too_large_for_exchange"
    assert preview["can_place"] is False


@pytest.mark.asyncio
async def test_approve_signal_rejects_when_lot_size_or_market_lot_size_max_qty_is_exceeded() -> None:
    gateway = FakeGateway(
        filters=make_filters(max_qty="0.05", market_max_qty="0.05"),
        place_order_results=[],
        place_algo_order_results=[],
        mark_price="100.07",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    with pytest.raises(ValueError, match="maximum"):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    assert_no_remote_submit_calls(gateway)


def test_build_execution_plan_rejects_when_rounded_net_r_falls_below_minimum() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters(), place_order_results=[]),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )

    execution = manager.build_execution_plan(
        symbol="BCHUSDT",
        account_snapshot=make_snapshot("100"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(),
        entry_style="STOP_ENTRY",
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
        mark_price=Decimal("99.5"),
        leverage_brackets=[
            LeverageBracket(
                bracket=1,
                initial_leverage=10,
                notional_cap=Decimal("100000"),
                notional_floor=Decimal("0"),
                maint_margin_ratio=Decimal("0.005"),
                cum=Decimal("0"),
            )
        ],
        estimated_cost=Decimal("0.2"),
    )

    assert execution["error"] == "net_r_multiple_below_min_after_rounding"
    assert float(execution["rounded_net_r_multiple"]) == pytest.approx(14.9 / 5.1)


@pytest.mark.asyncio
async def test_approve_signal_places_final_quantity_and_persists_structured_preview() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "101"}],
        place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
        account_info={
            "totalWalletBalance": "10",
            "availableBalance": "10",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        },
        mark_price="100.07",
        leverage_brackets=[
            LeverageBracket(
                bracket=1,
                initial_leverage=10,
                notional_cap=Decimal("100000"),
                notional_floor=Decimal("0"),
                maint_margin_ratio=Decimal("0.005"),
                cum=Decimal("0"),
            )
        ],
    )
    ws_manager = CapturingWebSocketManager()
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, ws_manager, notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    order = await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    assert gateway.place_order_calls[0]["price"] == "100.0"
    assert gateway.place_order_calls[0]["quantity"] == "0.059"
    assert gateway.place_order_calls[0]["timeInForce"] == "GTD"
    assert gateway.place_order_calls[0]["goodTillDate"] == str(int(order.expires_at.timestamp() * 1000))
    assert gateway.place_algo_order_calls[0]["algoType"] == "CONDITIONAL"
    assert gateway.place_algo_order_calls[0]["triggerPrice"] == "115.1"
    assert gateway.place_algo_order_calls[1]["triggerPrice"] == "95.1"
    assert order.entry_price == Decimal("100.0")
    assert order.stop_loss == Decimal("95.1")
    assert order.take_profit == Decimal("115.1")
    assert order.quantity == Decimal("0.059")
    assert order.position_margin == Decimal("2.952065")
    assert order.notional_value == Decimal("5.9")
    assert order.entry_order_id == "101"
    assert order.tp_order_id == "201"
    assert order.sl_order_id == "202"
    assert signal.status == SignalStatus.APPROVED
    assert signal.extra_context["order_preview"]["status"] == "resized_to_budget"
    assert signal.extra_context["order_preview"]["final_quantity"] == "0.059"
    assert signal.extra_context["order_preview"]["recommended_leverage"] == 2
    assert gateway.position_mode_probe_calls == 1
    assert gateway.position_mode_calls == []
    assert notifier.messages[0]["message"] == "BCHUSDT LONG at 100.0"
    assert ws_manager.events[0][0] == "order_status_change"
    assert session.committed is True


@pytest.mark.asyncio
async def test_approve_signal_submits_true_stop_entry_order_for_stop_style_signals() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[],
        place_algo_order_results=[{"algoId": "101"}, {"algoId": "201"}, {"algoId": "202"}],
        mark_price="99.5",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.entry_style = "STOP_ENTRY"
    signal.extra_context = {"entry_style": "STOP_ENTRY"}
    session = FakeSession(signal)

    order = await manager.approve_signal(session, signal_id=signal.id, approved_by="AUTO_MODE")

    assert order.entry_style == "STOP_ENTRY"
    assert gateway.place_order_calls == []
    assert gateway.place_algo_order_calls[0]["algoType"] == "CONDITIONAL"
    assert gateway.place_algo_order_calls[0]["type"] == "STOP"
    assert gateway.place_algo_order_calls[0]["price"] == "100.0"
    assert gateway.place_algo_order_calls[0]["triggerPrice"] == "100.0"
    assert gateway.place_algo_order_calls[0]["timeInForce"] == "GTC"
    assert "goodTillDate" not in gateway.place_algo_order_calls[0]
    assert gateway.place_algo_order_calls[0]["workingType"] == "MARK_PRICE"
    assert gateway.place_algo_order_calls[0]["clientAlgoId"] == "fbot.1.entry"
    assert order.entry_order_id == "101"
    assert order.strategy_context["entry_expiry_control"] == "internal_timer"


@pytest.mark.asyncio
async def test_approve_signal_falls_back_to_internal_expiry_when_exchange_gtd_is_rejected() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[
            BinanceAPIError('{"code": -1116, "msg": "Invalid timeInForce."}'),
            {"orderId": "101"},
        ],
        place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
        mark_price="100.07",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    order = await manager.approve_signal(session, signal_id=signal.id, approved_by="AUTO_MODE")

    assert order.entry_order_id == "101"
    assert len(gateway.place_order_calls) == 2
    assert gateway.place_order_calls[0]["timeInForce"] == "GTD"
    assert "goodTillDate" in gateway.place_order_calls[0]
    assert gateway.place_order_calls[1]["timeInForce"] == "GTC"
    assert "goodTillDate" not in gateway.place_order_calls[1]
    assert order.strategy_context["entry_expiry_control"] == "internal_timer"
    assert "Invalid timeInForce" in str(order.strategy_context.get("entry_gtd_fallback_error"))


@pytest.mark.asyncio
async def test_approve_signal_uses_internal_timer_when_expiry_is_inside_gtd_buffer() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "101"}],
        place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
        mark_price="100.07",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    session = FakeSession(signal)

    order = await manager.approve_signal(session, signal_id=signal.id, approved_by="AUTO_MODE")

    assert order.entry_order_id == "101"
    assert gateway.place_order_calls[0]["timeInForce"] == "GTC"
    assert "goodTillDate" not in gateway.place_order_calls[0]
    assert order.strategy_context["entry_expiry_control"] == "internal_timer"
    assert order.strategy_context["entry_gtd_requested"] == "true"
    assert order.strategy_context.get("entry_exchange_good_till_ms") is None


@pytest.mark.asyncio
async def test_approve_signal_skips_position_mode_change_when_account_is_already_one_way() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "101"}],
        place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
        mark_price="100.07",
        position_mode_value=False,
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    order = await manager.approve_signal(session, signal_id=signal.id, approved_by="AUTO_MODE")

    assert order.entry_order_id == "101"
    assert gateway.position_mode_probe_calls == 1
    assert gateway.position_mode_calls == []
    assert gateway.margin_type_calls == [("BCHUSDT", "ISOLATED")]


@pytest.mark.asyncio
async def test_approve_signal_uses_stop_distance_sizing_for_auto_mode_path() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "101"}],
        place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
        account_info={
            "totalWalletBalance": "100",
            "availableBalance": "100",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        },
        mark_price="100.07",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    order = await manager.approve_signal(
        session,
        signal_id=signal.id,
        approved_by="AUTO_MODE",
        settings_map_override={**DEFAULT_SETTINGS_MAP, "risk_per_trade_pct": "invalid"},
        risk_budget_override_usdt=Decimal("100"),
        use_stop_distance_position_sizing=True,
    )

    assert order.quantity == Decimal("0.397")
    assert Decimal(order.risk_budget_usdt) == Decimal("2")
    assert Decimal(order.risk_usdt_at_stop) > Decimal("1.99")
    assert Decimal(order.risk_usdt_at_stop) < Decimal("2.01")


def test_build_preview_includes_fee_and_slippage_in_stop_risk() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters(), place_order_results=[]),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )

    low_cost_preview = manager.build_preview(
        balance=Decimal("100"),
        settings_map={**DEFAULT_SETTINGS_MAP, "maker_fee_rate": "0.0001", "taker_fee_rate": "0.0001"},
        filters=make_filters(),
        entry_style="LIMIT_GTD",
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
        use_stop_distance_position_sizing=False,
    )
    high_cost_preview = manager.build_preview(
        balance=Decimal("100"),
        settings_map={**DEFAULT_SETTINGS_MAP, "maker_fee_rate": "0.0020", "taker_fee_rate": "0.0025"},
        filters=make_filters(),
        entry_style="STOP_ENTRY",
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
        use_stop_distance_position_sizing=False,
    )

    assert Decimal(low_cost_preview["stop_risk_execution_cost"]) > Decimal("0")
    assert Decimal(high_cost_preview["stop_risk_execution_cost"]) > Decimal(low_cost_preview["stop_risk_execution_cost"])
    assert Decimal(high_cost_preview["risk_usdt_at_stop"]) > Decimal(low_cost_preview["risk_usdt_at_stop"])


def test_build_preview_rejects_when_bracket_maintenance_margin_puts_liquidation_too_close_to_stop() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters(min_notional="20"), place_order_results=[]),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )

    preview = manager.build_preview(
        balance=Decimal("10"),
        settings_map=DEFAULT_SETTINGS_MAP,
        filters=make_filters(min_notional="20"),
        direction=SignalDirection.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        mark_price=Decimal("100"),
        leverage_brackets=[
            LeverageBracket(
                bracket=1,
                initial_leverage=10,
                notional_cap=Decimal("100000"),
                notional_floor=Decimal("0"),
                maint_margin_ratio=Decimal("0.08"),
                cum=Decimal("0"),
            )
        ],
    )

    assert preview["status"] == "not_affordable"
    assert preview["can_place"] is False
    assert Decimal(preview["maintenance_margin_ratio"]) == Decimal("0.08")
    assert Decimal(preview["liquidation_gap_pct"]) < Decimal(preview["required_liquidation_gap_pct"])


@pytest.mark.asyncio
async def test_approve_signal_uses_single_tp_for_aqrr_even_if_legacy_partial_flag_is_sent() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "101"}],
        place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
        account_info={
            "totalWalletBalance": "10",
            "availableBalance": "10",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        },
        mark_price="100.07",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    order = await manager.approve_signal(
        session,
        signal_id=signal.id,
        approved_by="AUTO_MODE",
        settings_map_override={**DEFAULT_SETTINGS_MAP, "auto_mode_partial_tp": "true"},
    )

    assert order.partial_tp_enabled is False
    assert order.take_profit_1 is None
    assert order.take_profit_2 is None
    assert order.tp_order_1_id is None
    assert order.tp_order_2_id is None
    assert order.tp_order_id == "201"
    assert order.sl_order_id == "202"
    assert gateway.place_algo_order_calls[0]["type"] == "TAKE_PROFIT_MARKET"
    assert gateway.place_algo_order_calls[0]["quantity"] == "0.059"
    assert gateway.place_algo_order_calls[1]["type"] == "STOP_MARKET"
    assert gateway.place_algo_order_calls[1]["quantity"] == "0.059"


@pytest.mark.asyncio
async def test_approve_signal_does_not_emit_partial_tp_fallback_audit_in_aqrr_mode() -> None:
    gateway = FakeGateway(
        filters=make_filters(min_notional="5"),
        place_order_results=[{"orderId": "101"}],
        place_algo_order_results=[{"algoId": "201"}, {"algoId": "202"}],
        account_info={
            "totalWalletBalance": "10",
            "availableBalance": "10",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        },
        mark_price="100.07",
    )
    gateway.filters = SymbolFilters(
        symbol=gateway.filters.symbol,
        tick_size=gateway.filters.tick_size,
        step_size=gateway.filters.step_size,
        min_qty=Decimal("0.06"),
        min_notional=gateway.filters.min_notional,
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    order = await manager.approve_signal(
        session,
        signal_id=signal.id,
        approved_by="AUTO_MODE",
        settings_map_override={**DEFAULT_SETTINGS_MAP, "auto_mode_partial_tp": "true"},
    )

    assert order.partial_tp_enabled is False
    assert order.tp_order_id == "201"
    assert order.tp_order_1_id is None
    assert order.tp_order_2_id is None
    assert gateway.place_algo_order_calls[0]["type"] == "TAKE_PROFIT_MARKET"
    assert not any(
        isinstance(item, AuditLog) and item.event_type == "ORDER_PARTIAL_TP_FALLBACK"
        for item in session.added
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "entry_price", "stop_loss", "take_profit", "mark_price", "expected_reason", "expected_message"),
    [
        (
            SignalDirection.SHORT,
            Decimal("475.2385"),
            Decimal("481.44"),
            Decimal("456.634"),
            "482.95",
            "stop_loss_crossed",
            "BCHUSDT order could not be placed because the stop-loss would immediately trigger on Binance at the current mark price. Mark 482.95, entry 475.3, stop-loss 481.5, take-profit 456.6.",
        ),
        (
            SignalDirection.SHORT,
            Decimal("475.2385"),
            Decimal("481.44"),
            Decimal("456.634"),
            "478.0",
            "entry_crossed",
            "BCHUSDT order could not be placed because the entry level has already been crossed and the pending LIMIT order would execute immediately. Mark 478, entry 475.3, stop-loss 481.5, take-profit 456.6.",
        ),
        (
            SignalDirection.SHORT,
            Decimal("475.2385"),
            Decimal("481.44"),
            Decimal("456.634"),
            "455.0",
            "take_profit_crossed",
            "BCHUSDT order could not be placed because the take-profit would immediately trigger on Binance at the current mark price. Mark 455, entry 475.3, stop-loss 481.5, take-profit 456.6.",
        ),
        (
            SignalDirection.LONG,
            Decimal("100.07"),
            Decimal("95.02"),
            Decimal("115.09"),
            "94.9",
            "stop_loss_crossed",
            "BCHUSDT order could not be placed because the stop-loss would immediately trigger on Binance at the current mark price. Mark 94.9, entry 100, stop-loss 95.1, take-profit 115.1.",
        ),
        (
            SignalDirection.LONG,
            Decimal("100.07"),
            Decimal("95.02"),
            Decimal("115.09"),
            "99.9",
            "entry_crossed",
            "BCHUSDT order could not be placed because the entry level has already been crossed and the pending LIMIT order would execute immediately. Mark 99.9, entry 100, stop-loss 95.1, take-profit 115.1.",
        ),
        (
            SignalDirection.LONG,
            Decimal("100.07"),
            Decimal("95.02"),
            Decimal("115.09"),
            "115.2",
            "take_profit_crossed",
            "BCHUSDT order could not be placed because the take-profit would immediately trigger on Binance at the current mark price. Mark 115.2, entry 100, stop-loss 95.1, take-profit 115.1.",
        ),
    ],
)
async def test_approve_signal_rejects_stale_mark_price_before_submitting_orders(
    direction: SignalDirection,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    mark_price: str,
    expected_reason: str,
    expected_message: str,
) -> None:
    gateway = FakeGateway(filters=make_filters(), place_order_results=[], place_algo_order_results=[], mark_price=mark_price)
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    session = FakeSession(signal)

    with pytest.raises(ValueError) as exc_info:
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    assert str(exc_info.value) == expected_message
    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_APPROVAL_FAILED")
    assert audit.message == expected_message
    assert audit.details["approved_by"] == "LEGACY_MODE"
    assert audit.details["mark_price"] == manager._decimal_string(Decimal(mark_price))
    assert audit.details["stale_reason"] == expected_reason
    assert audit.details["execution_prices"] == {
        "entry_price": "100" if direction == SignalDirection.LONG else "475.3",
        "stop_loss": "95.1" if direction == SignalDirection.LONG else "481.5",
        "take_profit": "115.1" if direction == SignalDirection.LONG else "456.6",
    }
    assert "order_preview" in audit.details
    assert signal.status == SignalStatus.QUALIFIED
    assert session.committed is True
    assert_no_remote_submit_calls(gateway)


@pytest.mark.asyncio
async def test_approve_signal_preview_failure_preserves_raw_aqrr_reason_context() -> None:
    gateway = FakeGateway(
        filters=make_filters(min_notional="20"),
        place_order_results=[],
        account_info={
            "totalWalletBalance": "1",
            "availableBalance": "1",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        },
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "pullback_no_rejection_evidence",
        "aqrr_raw_rejection_reasons": ["pullback_no_rejection_evidence"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "pullback_continuation",
        "entry_style": "LIMIT_GTD",
    }
    session = FakeSession(signal)

    with pytest.raises(ValueError):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_APPROVAL_FAILED")

    assert audit.details["raw_aqrr_reason"] == "pullback_no_rejection_evidence"
    assert audit.details["raw_aqrr_reasons"] == ["pullback_no_rejection_evidence"]
    assert audit.details["aqrr_rejection_stage"] == "candidate_build"
    assert audit.details["setup_family"] == "pullback_continuation"
    assert audit.details["entry_style"] == "LIMIT_GTD"


@pytest.mark.asyncio
async def test_approve_signal_submission_failure_preserves_aqrr_context() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[],
        place_algo_order_results=[BinanceAPIError('{"code": -2019, "msg": "Margin is insufficient."}')],
        mark_price="99.0",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "pullback_no_rejection_evidence",
        "aqrr_raw_rejection_reasons": ["pullback_no_rejection_evidence"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "pullback_continuation",
        "entry_style": "STOP_ENTRY",
    }
    session = FakeSession(signal)

    with pytest.raises(OrderApprovalExchangeError):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_SUBMISSION_FAILED")

    assert audit.details["reason"] == "submission_failed"
    assert audit.details["raw_aqrr_reason"] == "pullback_no_rejection_evidence"
    assert audit.details["raw_aqrr_reasons"] == ["pullback_no_rejection_evidence"]
    assert audit.details["aqrr_rejection_stage"] == "candidate_build"
    assert audit.details["setup_family"] == "pullback_continuation"
    assert audit.details["entry_style"] == "STOP_ENTRY"
    assert audit.details["order_route"] == "algo"


@pytest.mark.asyncio
async def test_approve_signal_attempts_one_safe_recalculation_after_exchange_filter_reject() -> None:
    gateway = RecalculationGateway(
        filters=make_filters(),
        first_error=BinanceAPIError('{"code": -2019, "msg": "Margin is insufficient."}'),
        account_info_after_recalc={
            "totalWalletBalance": "10",
            "availableBalance": "6",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        },
        retry_result={"orderId": "101"},
        mark_price="100.07",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    order = await manager.approve_signal(session, signal_id=signal.id, approved_by="AUTO_MODE")

    assert len(gateway.place_order_calls) == 2
    assert gateway.leverage_calls[:2] == [("BCHUSDT", 2), ("BCHUSDT", 3)]
    assert order.entry_order_id == "101"
    assert any(
        isinstance(item, AuditLog) and item.event_type == "ORDER_SUBMISSION_RECALCULATED"
        for item in session.added
    )


@pytest.mark.asyncio
async def test_approve_signal_rejects_cleanly_after_single_invalid_recalculation() -> None:
    gateway = RecalculationGateway(
        filters=make_filters(),
        first_error=BinanceAPIError('{"code": -2019, "msg": "Margin is insufficient."}'),
        account_info_after_recalc={
            "totalWalletBalance": "10",
            "availableBalance": "6",
            "totalInitialMargin": "0",
            "totalOpenOrderInitialMargin": "0",
            "totalPositionInitialMargin": "0",
        },
        retry_result=BinanceAPIError('{"code": -2019, "msg": "Margin is insufficient."}'),
        mark_price="100.07",
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    with pytest.raises(OrderApprovalExchangeError):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="AUTO_MODE")

    assert len(gateway.place_order_calls) == 2
    assert len([item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_SUBMISSION_RECALCULATED"]) == 1


@pytest.mark.asyncio
async def test_approve_signal_rejects_signals_that_are_not_from_latest_completed_scan() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters()),
        CapturingWebSocketManager(),
        FakeNotifier(),
        latest_completed_scan_id=8,
    )
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
        scan_cycle_id=7,
    )
    session = FakeSession(signal)

    with pytest.raises(ValueError, match="latest completed scan"):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")


@pytest.mark.asyncio
async def test_approve_signal_rejects_fourth_shared_entry_order() -> None:
    active_orders = [
        Order(id=11, symbol="ONEUSDT", direction=SignalDirection.LONG, leverage=4, entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("110"), quantity=Decimal("1"), position_margin=Decimal("10"), notional_value=Decimal("40"), rr_ratio=Decimal("2.0"), status=OrderStatus.ORDER_PLACED, expires_at=datetime.now(timezone.utc) + timedelta(hours=1), risk_budget_usdt=Decimal("2"), risk_usdt_at_stop=Decimal("1"), risk_pct_of_wallet=Decimal("10"), approved_by="LEGACY_MODE"),
        Order(id=12, symbol="TWOUSDT", direction=SignalDirection.LONG, leverage=4, entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("110"), quantity=Decimal("1"), position_margin=Decimal("10"), notional_value=Decimal("40"), rr_ratio=Decimal("2.0"), status=OrderStatus.IN_POSITION, expires_at=datetime.now(timezone.utc) + timedelta(hours=1), risk_budget_usdt=Decimal("2"), risk_usdt_at_stop=Decimal("1"), risk_pct_of_wallet=Decimal("10"), approved_by="AUTO_MODE"),
        Order(id=13, symbol="THREEUSDT", direction=SignalDirection.SHORT, leverage=4, entry_price=Decimal("100"), stop_loss=Decimal("105"), take_profit=Decimal("90"), quantity=Decimal("1"), position_margin=Decimal("10"), notional_value=Decimal("40"), rr_ratio=Decimal("2.0"), status=OrderStatus.ORDER_PLACED, expires_at=datetime.now(timezone.utc) + timedelta(hours=1), risk_budget_usdt=Decimal("2"), risk_usdt_at_stop=Decimal("1"), risk_pct_of_wallet=Decimal("10"), approved_by="AUTO_MODE"),
    ]
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters(), place_order_results=[]),
        CapturingWebSocketManager(),
        FakeNotifier(),
        active_orders=active_orders,
    )
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    with pytest.raises(ValueError, match="shared entry slots"):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_APPROVAL_FAILED")
    assert audit.details["approved_by"] == "LEGACY_MODE"
    assert audit.details["active_entry_order_count"] == 3
    assert audit.details["slot_cap"] == 3
    assert_no_remote_submit_calls(manager.gateway)


@pytest.mark.asyncio
async def test_approve_signal_rejects_same_symbol_when_active_entry_exists() -> None:
    active_orders = [
        Order(
            id=11,
            symbol="BCHUSDT",
            direction=SignalDirection.LONG,
            leverage=4,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("110"),
            quantity=Decimal("1"),
            position_margin=Decimal("10"),
            notional_value=Decimal("40"),
            rr_ratio=Decimal("2.0"),
            status=OrderStatus.ORDER_PLACED,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            risk_budget_usdt=Decimal("2"),
            risk_usdt_at_stop=Decimal("1"),
            risk_pct_of_wallet=Decimal("10"),
            approved_by="AUTO_MODE",
        ),
    ]
    manager = ApprovalOrderManager(
        FakeGateway(filters=make_filters(), place_order_results=[]),
        CapturingWebSocketManager(),
        FakeNotifier(),
        active_orders=active_orders,
    )
    signal = make_signal(
        direction=SignalDirection.SHORT,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("105.02"),
        take_profit=Decimal("90.09"),
    )
    session = FakeSession(signal)

    with pytest.raises(ValueError, match="active entry order"):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_APPROVAL_FAILED")
    assert audit.details["approved_by"] == "LEGACY_MODE"
    assert "BCHUSDT" in audit.details["active_symbols"]
    assert_no_remote_submit_calls(manager.gateway)


@pytest.mark.asyncio
async def test_approve_signal_rejects_same_symbol_when_exchange_position_exists() -> None:
    manager = ApprovalOrderManager(
        FakeGateway(
            filters=make_filters(),
            place_order_results=[],
            positions_payload=[
                {
                    "symbol": "BCHUSDT",
                    "positionAmt": "0.75",
                    "positionSide": "BOTH",
                }
            ],
        ),
        CapturingWebSocketManager(),
        FakeNotifier(),
    )
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    session = FakeSession(signal)

    with pytest.raises(ValueError, match="open Binance position"):
        await manager.approve_signal(session, signal_id=signal.id, approved_by="LEGACY_MODE")

    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_APPROVAL_FAILED")
    assert audit.details["approved_by"] == "LEGACY_MODE"
    assert audit.details["open_position_symbols"] == ["BCHUSDT"]
    assert_no_remote_submit_calls(manager.gateway)


@pytest.mark.asyncio
async def test_cancel_order_uses_standard_entry_cancel_and_algo_protection_cancels() -> None:
    gateway = FakeGateway(filters=make_filters())
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="LEGACY_MODE",
    )
    session = FakeSession(signal, order=order)

    cancelled = await manager.cancel_order(session, order_id=order.id)

    assert cancelled.status == OrderStatus.CANCELLED_BY_USER
    assert gateway.cancel_order_calls == [("BCHUSDT", "101")]
    assert gateway.cancel_algo_order_calls == ["201", "202"]


@pytest.mark.asyncio
async def test_cancel_order_cancels_both_partial_tp_legs() -> None:
    gateway = FakeGateway(filters=make_filters())
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = make_partial_order(signal=signal, status=OrderStatus.ORDER_PLACED)
    session = FakeSession(signal, order=order)

    cancelled = await manager.cancel_order(session, order_id=order.id)

    assert cancelled.status == OrderStatus.CANCELLED_BY_USER
    assert gateway.cancel_order_calls == [("BCHUSDT", "101")]
    assert gateway.cancel_algo_order_calls == ["201", "202", "203"]


@pytest.mark.asyncio
async def test_cancel_order_uses_algo_cancel_for_untriggered_stop_entry_and_preserves_reason_context() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_algo_order_results={"101": {"algoStatus": "NEW", "actualOrderId": ""}},
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        entry_style="STOP_ENTRY",
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "pullback_no_rejection_evidence",
        "aqrr_raw_rejection_reasons": ["pullback_no_rejection_evidence"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "pullback_continuation",
        "entry_style": "STOP_ENTRY",
    }
    session = FakeSession(signal, order=order)

    cancelled = await manager.cancel_order(
        session,
        order_id=order.id,
        reason="viability_lost",
        reason_context={
            "lifecycle_reason": "viability_lost",
            "raw_aqrr_reason": "pullback_no_rejection_evidence",
            "raw_aqrr_reasons": ["pullback_no_rejection_evidence"],
        },
    )

    assert cancelled.status == OrderStatus.CANCELLED_BY_BOT
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["101", "201", "202"]
    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CANCELLED")
    assert audit.details["reason"] == "viability_lost"
    assert audit.details["lifecycle_reason"] == "viability_lost"
    assert audit.details["raw_aqrr_reason"] == "pullback_no_rejection_evidence"
    assert audit.details["aqrr_rejection_stage"] == "candidate_build"


@pytest.mark.asyncio
async def test_cancel_order_maps_legacy_pending_reason_to_explicit_policy() -> None:
    gateway = FakeGateway(filters=make_filters())
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    cancelled = await manager.cancel_order(session, order_id=order.id, reason="auto_mode_rebalanced")

    assert cancelled.cancel_reason == "setup_state_changed"
    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CANCELLED")
    assert audit.details["reason"] == "setup_state_changed"
    assert audit.details["legacy_reason"] == "auto_mode_rebalanced"


@pytest.mark.asyncio
async def test_cancel_order_rejects_unsupported_pending_cancel_reason() -> None:
    gateway = FakeGateway(filters=make_filters())
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    with pytest.raises(ValueError, match="Unsupported pending entry cancellation reason"):
        await manager.cancel_order(session, order_id=order.id, reason="invalidated")

    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == []


@pytest.mark.asyncio
async def test_cancel_order_terminally_resolves_expired_pending_when_recovery_is_inconclusive() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_order_results={
            "101": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "fbot.12.entry": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
        },
    )

    async def failing_positions(_credentials):
        raise RuntimeError("positions unavailable")

    gateway.positions = failing_positions
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
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
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    cancelled = await manager.cancel_order(session, order_id=order.id, reason="expired")

    assert cancelled.status == OrderStatus.CANCELLED_BY_BOT
    assert cancelled.cancel_reason == "expired"
    assert gateway.cancel_order_calls == [("BCHUSDT", "101")]
    cancellation_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CANCELLED")
    assert cancellation_audit.details["reason"] == "expired"
    assert cancellation_audit.details["authoritative_recovery_outcome"] == "authoritative_recovery_inconclusive"
    assert cancellation_audit.details["expiry_resolution_path"] == "terminal_on_inconclusive_recovery"


@pytest.mark.asyncio
async def test_sync_order_moves_partially_filled_limit_entry_into_live_state_and_resizes_protections() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_order_results={
            "101": {
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.120",
                "origQty": "0.358",
                "updateTime": now_ms,
            }
        },
        query_algo_order_results={
            "212": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            }
        },
        account_trade_results={
            "101": [
                {
                    "qty": "0.120",
                    "price": "100.3",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.entry_price == Decimal("100.3")
    assert synced.tp_order_id == "211"
    assert synced.sl_order_id == "212"
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert Decimal(gateway.place_algo_order_calls[-2]["quantity"]) == Decimal("0.120")
    assert Decimal(gateway.place_algo_order_calls[-1]["quantity"]) == Decimal("0.120")
    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert audit.message == "BCHUSDT position partially opened"
    assert audit.details["entry_status"] == "PARTIALLY_FILLED"
    assert audit.details["entry_remainder_cancelled"] is False
    assert notifier.messages[0]["message"] == "BCHUSDT position partially opened"


@pytest.mark.asyncio
async def test_activate_entry_fill_closes_too_small_partial_fill_instead_of_leaving_micro_position() -> None:
    class MicroFillManager(ApprovalOrderManager):
        def __init__(self, gateway, ws_manager, notifier):
            super().__init__(gateway, ws_manager, notifier)
            self.flatten_reasons: list[str] = []
            self.protection_calls = 0

        async def _entry_fill_details(self, credentials, order, *, entry_state):
            return Decimal("0.040"), Decimal("100.0")

        async def _ensure_live_protections(self, credentials, order, *, live_quantity, force_confirm_stop_loss):
            self.protection_calls += 1
            return True

        async def _flatten_live_order(self, session, *, credentials, order, scan_cycle_id, reason, reason_context=None):
            self.flatten_reasons.append(reason)
            order.status = OrderStatus.CANCELLED_BY_BOT
            return order

        async def _signal_reason_context(self, session, *, signal_id=None, signal=None, setup_family=None, entry_style=None):
            return {}

    gateway = FakeGateway(filters=make_filters(min_notional="5"))
    manager = MicroFillManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = make_entry_fill_order(signal=signal, status=OrderStatus.ORDER_PLACED)
    session = FakeSession(signal, order=order)

    activated = await manager._activate_entry_fill(
        session,
        credentials=SimpleNamespace(api_key="key", private_key_pem="private"),
        order=order,
        entry_state=EntryOrderState(remote_kind="standard", state={}, status="PARTIALLY_FILLED"),
        scan_cycle_id=signal.scan_cycle_id,
    )

    assert activated is False
    assert manager.flatten_reasons == ["minimum_viable_partial_fill"]
    assert manager.protection_calls == 0
    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_MINIMUM_VIABLE_FILL_CLOSED")
    assert audit.details["normalized_notional"] == "4"


@pytest.mark.asyncio
async def test_sync_order_moves_partially_filled_stop_entry_actual_order_into_live_state_and_resizes_protections() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_algo_order_results={
            "101": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "301",
            },
            "212": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            },
        },
        query_order_results={
            "301": {
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.120",
                "origQty": "0.358",
                "updateTime": now_ms,
            }
        },
        account_trade_results={
            "301": [
                {
                    "qty": "0.120",
                    "price": "100.4",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        entry_style="STOP_ENTRY",
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.entry_price == Decimal("100.4")
    assert synced.tp_order_id == "211"
    assert synced.sl_order_id == "212"
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert Decimal(gateway.place_algo_order_calls[-2]["quantity"]) == Decimal("0.120")
    assert Decimal(gateway.place_algo_order_calls[-1]["quantity"]) == Decimal("0.120")
    audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert audit.message == "BCHUSDT position partially opened"
    assert audit.details["actual_order_id"] == "301"
    assert audit.details["entry_remainder_cancelled"] is False
    assert notifier.messages[0]["message"] == "BCHUSDT position partially opened"


@pytest.mark.asyncio
async def test_sync_order_flattens_live_fill_when_stop_loss_protection_cannot_be_confirmed() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "901", "avgPrice": "99.0"}],
        query_order_results={
            "101": {
                "status": "FILLED",
                "executedQty": "0.358",
                "origQty": "0.358",
                "avgPrice": "100.0",
                "updateTime": now_ms,
            }
        },
        query_algo_order_results={
            "202": {
                "algoStatus": "CANCELED",
                "actualOrderId": "",
            }
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.CLOSED_BY_BOT
    assert synced.close_type == "BOT"
    assert synced.remaining_quantity == Decimal("0")
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert gateway.place_order_calls[0]["type"] == "MARKET"
    assert gateway.place_order_calls[0]["reduceOnly"] == "true"
    protection_failure = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_PROTECTION_FAILURE"
    )
    assert protection_failure.details["reason"] == "protection_confirmation_failed"
    assert protection_failure.details["lifecycle_reason"] == "protection_confirmation_failed"
    assert protection_failure.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    close_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_BY_BOT")
    assert close_audit.details["reason"] == "protection_confirmation_failed"
    assert close_audit.details["lifecycle_reason"] == "protection_confirmation_failed"
    assert close_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED" for item in session.added)


@pytest.mark.asyncio
async def test_sync_order_flattens_live_position_when_stop_loss_protection_becomes_inactive_after_confirmation() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        query_order_results={
            "101": {
                "status": "FILLED",
                "executedQty": "0.358",
                "origQty": "0.358",
                "avgPrice": "100.0",
                "updateTime": now_ms,
            }
        },
        query_algo_order_results={
            "201": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            },
            "202": {
                "algoStatus": "CANCELED",
                "actualOrderId": "",
            },
        },
        place_order_results=[{"orderId": "901", "avgPrice": "99.0"}],
        account_trade_results={
            "101": [
                {
                    "qty": "0.358",
                    "price": "100.0",
                    "time": now_ms,
                }
            ],
            "901": [
                {
                    "qty": "0.358",
                    "price": "99.0",
                    "realizedPnl": "-0.358",
                    "time": now_ms,
                }
            ],
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
        strategy_context={
            "entry_filled_quantity": "0.358",
            "protection_quantity": "0.358",
        },
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.CLOSED_BY_BOT
    assert synced.close_type == "BOT"
    assert synced.remaining_quantity == Decimal("0")
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert gateway.place_order_calls[0]["type"] == "MARKET"
    assert gateway.place_order_calls[0]["quantity"] == "0.358"
    assert gateway.place_order_calls[0]["reduceOnly"] == "true"
    protection_failure = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_PROTECTION_FAILURE"
    )
    assert protection_failure.details["reason"] == "stop_loss_protection_inactive"
    assert protection_failure.details["lifecycle_reason"] == "stop_loss_protection_inactive"
    assert protection_failure.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert protection_failure.details["aqrr_rejection_stage"] == "candidate_build"
    close_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_BY_BOT")
    assert close_audit.details["reason"] == "stop_loss_protection_inactive"
    assert close_audit.details["lifecycle_reason"] == "stop_loss_protection_inactive"
    assert close_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert close_audit.details["aqrr_rejection_stage"] == "candidate_build"
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_PROTECTION_INACTIVE" for item in session.added)


@pytest.mark.asyncio
async def test_sync_order_cancels_only_expired_limit_entry_remainder_after_partial_fill() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        query_order_results={
            "101": {
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.120",
                "origQty": "0.358",
                "avgPrice": "100.3",
                "updateTime": now_ms,
            }
        },
        query_algo_order_results={
            "201": {
                "algoStatus": "WORKING",
                "actualOrderId": "",
            },
            "202": {
                "algoStatus": "WORKING",
                "actualOrderId": "",
            },
        },
        account_trade_results={
            "101": [
                {
                    "qty": "0.120",
                    "price": "100.3",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        approved_by="AUTO_MODE",
        strategy_context={
            "entry_filled_quantity": "0.120",
            "protection_quantity": "0.120",
        },
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert gateway.cancel_order_calls == [("BCHUSDT", "101")]
    assert gateway.cancel_algo_order_calls == []
    remainder_cancelled = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_ENTRY_REMAINDER_CANCELLED"
    )
    assert remainder_cancelled.details["reason"] == "entry_expired_after_partial_fill"
    assert remainder_cancelled.details["filled_quantity"] == "0.12"
    assert remainder_cancelled.details["remaining_live_quantity"] == "0.12"
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_BY_BOT" for item in session.added)


@pytest.mark.asyncio
async def test_sync_order_cancels_only_expired_stop_entry_actual_remainder_after_partial_fill() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        query_algo_order_results={
            "101": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "301",
            },
            "201": {
                "algoStatus": "WORKING",
                "actualOrderId": "",
            },
            "202": {
                "algoStatus": "WORKING",
                "actualOrderId": "",
            },
        },
        query_order_results={
            "301": {
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.120",
                "origQty": "0.358",
                "avgPrice": "100.4",
                "updateTime": now_ms,
            }
        },
        account_trade_results={
            "301": [
                {
                    "qty": "0.120",
                    "price": "100.4",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        entry_style="STOP_ENTRY",
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        approved_by="AUTO_MODE",
        strategy_context={
            "entry_filled_quantity": "0.120",
            "protection_quantity": "0.120",
        },
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert gateway.cancel_order_calls == [("BCHUSDT", "301")]
    assert gateway.cancel_algo_order_calls == []
    remainder_cancelled = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_ENTRY_REMAINDER_CANCELLED"
    )
    assert remainder_cancelled.details["reason"] == "entry_expired_after_partial_fill"
    assert remainder_cancelled.details["filled_quantity"] == "0.12"
    assert remainder_cancelled.details["remaining_live_quantity"] == "0.12"
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_BY_BOT" for item in session.added)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "expected_reason"),
    [
        ("CANCELED", "entry_cancelled_after_partial_fill"),
        ("EXPIRED", "entry_expired_after_partial_fill"),
    ],
)
async def test_sync_order_recovers_terminal_limit_entry_partial_fill_and_replaces_protections_same_pass(
    terminal_status: str,
    expected_reason: str,
) -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_order_results={
            "101": {
                "status": terminal_status,
                "executedQty": "0.120",
                "origQty": "0.358",
                "avgPrice": "100.3",
                "updateTime": now_ms,
            }
        },
        account_trade_results={
            "101": [
                {
                    "qty": "0.120",
                    "price": "100.3",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = make_entry_fill_order(signal=signal, status=OrderStatus.ORDER_PLACED)
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.entry_price == Decimal("100.3")
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert gateway.place_algo_order_calls[-2]["quantity"] == "0.120"
    assert gateway.place_algo_order_calls[-1]["quantity"] == "0.120"

    remainder_cancelled = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_ENTRY_REMAINDER_CANCELLED"
    )
    assert remainder_cancelled.details["reason"] == expected_reason
    assert remainder_cancelled.details["entry_remainder_quantity"] == "0.238"
    assert remainder_cancelled.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"

    trigger_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert trigger_audit.details["entry_remainder_cancelled"] is True
    assert trigger_audit.details["entry_remainder_reason"] == expected_reason
    assert trigger_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert notifier.messages[0]["message"] == "BCHUSDT position partially opened"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "expected_reason"),
    [
        ("CANCELED", "entry_cancelled_after_partial_fill"),
        ("EXPIRED", "entry_expired_after_partial_fill"),
    ],
)
async def test_sync_order_recovers_terminal_stop_entry_actual_order_partial_fill_and_replaces_protections_same_pass(
    terminal_status: str,
    expected_reason: str,
) -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_algo_order_results={
            "101": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "301",
            }
        },
        query_order_results={
            "301": {
                "status": terminal_status,
                "executedQty": "0.120",
                "origQty": "0.358",
                "avgPrice": "100.4",
                "updateTime": now_ms,
            }
        },
        account_trade_results={
            "301": [
                {
                    "qty": "0.120",
                    "price": "100.4",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "STOP_ENTRY",
    }
    order = make_entry_fill_order(
        signal=signal,
        status=OrderStatus.ORDER_PLACED,
        entry_style="STOP_ENTRY",
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.entry_price == Decimal("100.4")
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert gateway.place_algo_order_calls[-2]["quantity"] == "0.120"
    assert gateway.place_algo_order_calls[-1]["quantity"] == "0.120"

    remainder_cancelled = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_ENTRY_REMAINDER_CANCELLED"
    )
    assert remainder_cancelled.details["reason"] == expected_reason
    assert remainder_cancelled.details["entry_route"] == "algo"
    assert remainder_cancelled.details["actual_order_id"] == "301"
    assert remainder_cancelled.details["entry_remainder_quantity"] == "0.238"
    assert remainder_cancelled.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"

    trigger_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert trigger_audit.details["entry_remainder_cancelled"] is True
    assert trigger_audit.details["entry_remainder_reason"] == expected_reason
    assert trigger_audit.details["entry_route"] == "algo"
    assert trigger_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert notifier.messages[0]["message"] == "BCHUSDT position partially opened"


@pytest.mark.asyncio
async def test_sync_order_handles_first_observed_partial_fill_after_expiry_in_one_pass() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_order_results={
            "101": {
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.120",
                "origQty": "0.358",
                "avgPrice": "100.3",
                "updateTime": now_ms,
            }
        },
        account_trade_results={
            "101": [
                {
                    "qty": "0.120",
                    "price": "100.3",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = make_entry_fill_order(
        signal=signal,
        status=OrderStatus.ORDER_PLACED,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.entry_price == Decimal("100.3")
    assert gateway.cancel_order_calls == [("BCHUSDT", "101")]
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert gateway.place_algo_order_calls[-2]["quantity"] == "0.120"
    assert gateway.place_algo_order_calls[-1]["quantity"] == "0.120"

    remainder_cancelled = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_ENTRY_REMAINDER_CANCELLED"
    )
    assert remainder_cancelled.details["reason"] == "entry_expired_after_partial_fill"
    assert remainder_cancelled.details["entry_remainder_quantity"] == "0.238"
    assert remainder_cancelled.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"

    trigger_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert trigger_audit.details["entry_remainder_cancelled"] is True
    assert trigger_audit.details["entry_remainder_reason"] == "entry_expired_after_partial_fill"
    assert trigger_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert notifier.messages[0]["message"] == "BCHUSDT position partially opened"


@pytest.mark.asyncio
async def test_sync_order_uses_authoritative_expiry_metadata_for_cancelled_entry_resolution() -> None:
    now = datetime.now(timezone.utc)
    now_ms = str(int(now.timestamp() * 1000))
    context_expiry = now - timedelta(minutes=2)
    gateway = FakeGateway(
        filters=make_filters(),
        query_order_results={
            "101": {
                "status": "CANCELED",
                "executedQty": "0",
                "origQty": "0.358",
                "updateTime": now_ms,
            }
        },
        positions_payload=[],
    )
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = make_entry_fill_order(
        signal=signal,
        status=OrderStatus.ORDER_PLACED,
        expires_at=now + timedelta(hours=6),
    )
    order.strategy_context = {
        "entry_expiry_at": context_expiry.isoformat(),
        "entry_expiry_epoch_ms": str(int(context_expiry.timestamp() * 1000)),
        "entry_expiry_control": "internal_timer",
    }
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.CANCELLED_BY_BOT
    assert synced.cancel_reason == "expired"
    assert synced.expires_at <= datetime.now(timezone.utc)
    assert synced.strategy_context["entry_expiry_epoch_ms"] == str(int(synced.expires_at.timestamp() * 1000))
    cancelled = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CANCELLED")
    assert cancelled.details["reason"] == "expired"
    assert cancelled.details["exchange_entry_status"] == "CANCELED"
    assert cancelled.details["exchange_cancel_cause"] == "authoritative_local_expiry_elapsed"


@pytest.mark.asyncio
async def test_sync_order_recovers_live_partial_fill_from_authoritative_position_when_entry_lookup_is_unresolved() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_order_results={
            "101": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "fbot.12.entry": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
        },
        query_algo_order_results={
            "212": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            }
        },
        positions_payload=[
            {
                "symbol": "BCHUSDT",
                "positionAmt": "0.120",
                "entryPrice": "100.55",
                "markPrice": "101.0",
                "unRealizedProfit": "0",
            }
        ],
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = make_entry_fill_order(signal=signal, status=OrderStatus.ORDER_PLACED)
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.entry_price == Decimal("100.55")
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert Decimal(gateway.place_algo_order_calls[-2]["quantity"]) == Decimal("0.120")
    assert Decimal(gateway.place_algo_order_calls[-1]["quantity"]) == Decimal("0.120")
    trigger_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert trigger_audit.details["entry_route"] == "authoritative_position"
    assert trigger_audit.details["entry_status"] == "PARTIALLY_FILLED"
    assert trigger_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert notifier.messages[0]["message"] == "BCHUSDT position partially opened"


@pytest.mark.asyncio
async def test_sync_order_recovers_live_partial_fill_from_authoritative_position_when_stop_entry_algo_lookup_is_unresolved() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_algo_order_results={
            "101": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "fbot.12.entry": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "212": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            },
        },
        positions_payload=[
            {
                "symbol": "BCHUSDT",
                "positionAmt": "0.120",
                "entryPrice": "100.45",
                "markPrice": "101.0",
                "unRealizedProfit": "0",
            }
        ],
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "STOP_ENTRY",
    }
    order = make_entry_fill_order(signal=signal, status=OrderStatus.ORDER_PLACED, entry_style="STOP_ENTRY")
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.entry_price == Decimal("100.45")
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert Decimal(gateway.place_algo_order_calls[-2]["quantity"]) == Decimal("0.120")
    assert Decimal(gateway.place_algo_order_calls[-1]["quantity"]) == Decimal("0.120")
    trigger_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert trigger_audit.details["entry_route"] == "authoritative_position"
    assert trigger_audit.details["entry_status"] == "PARTIALLY_FILLED"
    assert trigger_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert notifier.messages[0]["message"] == "BCHUSDT position partially opened"


@pytest.mark.asyncio
async def test_sync_order_recovers_stale_submitting_limit_order_from_authoritative_live_exposure() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_order_results={
            "101": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "fbot.12.entry": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
        },
        query_algo_order_results={
            "212": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            }
        },
        positions_payload=[
            {
                "symbol": "BCHUSDT",
                "positionAmt": "0.120",
                "entryPrice": "100.55",
                "markPrice": "101.0",
                "unRealizedProfit": "0",
            }
        ],
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = make_stale(make_entry_fill_order(signal=signal, status=OrderStatus.SUBMITTING))
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.cancel_reason is None
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.place_order_calls == []
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert Decimal(gateway.place_algo_order_calls[-2]["quantity"]) == Decimal("0.120")
    assert Decimal(gateway.place_algo_order_calls[-1]["quantity"]) == Decimal("0.120")
    trigger_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert trigger_audit.details["authoritative_recovery_outcome"] == "recovered_live_exposure"
    assert trigger_audit.details["entry_route"] == "authoritative_position"
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_SUBMISSION_FAILED" for item in session.added)


@pytest.mark.asyncio
async def test_sync_order_recovers_stale_submitting_stop_entry_from_authoritative_live_exposure() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_algo_order_results={
            "101": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "fbot.12.entry": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "212": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            },
        },
        positions_payload=[
            {
                "symbol": "BCHUSDT",
                "positionAmt": "0.120",
                "entryPrice": "100.45",
                "markPrice": "101.0",
                "unRealizedProfit": "0",
            }
        ],
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "STOP_ENTRY",
    }
    order = make_stale(make_entry_fill_order(signal=signal, status=OrderStatus.SUBMITTING, entry_style="STOP_ENTRY"))
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.cancel_reason is None
    assert synced.remaining_quantity == Decimal("0.120")
    assert synced.strategy_context["entry_filled_quantity"] == "0.12"
    assert synced.strategy_context["protection_quantity"] == "0.12"
    assert gateway.place_order_calls == []
    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    trigger_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_TRIGGERED")
    assert trigger_audit.details["authoritative_recovery_outcome"] == "recovered_live_exposure"
    assert trigger_audit.details["entry_route"] == "authoritative_position"
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_SUBMISSION_FAILED" for item in session.added)


@pytest.mark.asyncio
async def test_sync_order_marks_stale_submitting_order_failed_when_authoritative_checks_confirm_no_exposure() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_order_results={
            "101": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "fbot.12.entry": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
        },
        positions_payload=[],
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = make_stale(make_entry_fill_order(signal=signal, status=OrderStatus.SUBMITTING))
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.CANCELLED_BY_BOT
    assert synced.cancel_reason == "submission_failed"
    assert gateway.place_algo_order_calls == []
    assert gateway.place_order_calls == []
    submission_failed = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_SUBMISSION_FAILED")
    assert submission_failed.details["authoritative_recovery_outcome"] == "confirmed_no_exposure"
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_AUTHORITATIVE_RECOVERY_INCONCLUSIVE" for item in session.added)


@pytest.mark.asyncio
async def test_sync_order_keeps_stale_submitting_order_recoverable_when_authoritative_checks_are_inconclusive() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_order_results={
            "101": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
            "fbot.12.entry": BinanceAPIError('{"code": -2013, "msg": "Order does not exist."}'),
        },
    )

    async def failing_positions(_credentials):
        raise RuntimeError("positions unavailable")

    gateway.positions = failing_positions
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = make_stale(make_entry_fill_order(signal=signal, status=OrderStatus.SUBMITTING))
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.SUBMITTING
    assert synced.cancel_reason is None
    assert synced.cancelled_at is None
    assert gateway.place_algo_order_calls == []
    assert gateway.place_order_calls == []
    assert not any(isinstance(item, AuditLog) and item.event_type == "ORDER_SUBMISSION_FAILED" for item in session.added)
    inconclusive_audit = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_AUTHORITATIVE_RECOVERY_INCONCLUSIVE"
    )
    assert inconclusive_audit.details["authoritative_recovery_outcome"] == "authoritative_recovery_inconclusive"
    assert inconclusive_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"


@pytest.mark.asyncio
async def test_close_position_uses_remote_live_quantity_when_fill_context_is_missing() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "901", "avgPrice": "106.5"}],
        positions_payload=[
            {
                "symbol": "BCHUSDT",
                "positionAmt": "0.120",
                "entryPrice": "100.25",
                "markPrice": "106.5",
                "unRealizedProfit": "0",
            }
        ],
        account_trade_results={
            "901": [
                {
                    "qty": "0.120",
                    "price": "106.5",
                    "realizedPnl": "0.75",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    ws_manager = CapturingWebSocketManager()
    manager = ApprovalOrderManager(gateway, ws_manager, notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.25"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=None,
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    closed = await manager.close_position(
        session,
        order_id=order.id,
        reason="aqrr_regime_flip",
        reason_context={
            "lifecycle_reason": "aqrr_regime_flip",
            "raw_aqrr_reason": "breakout_not_closed_through_level",
            "raw_aqrr_reasons": ["breakout_not_closed_through_level"],
        },
    )

    assert closed.status == OrderStatus.CLOSED_BY_BOT
    assert closed.close_type == "BOT"
    assert closed.remaining_quantity == Decimal("0")
    assert closed.realized_pnl == Decimal("0.75")
    assert gateway.cancel_algo_order_calls == ["201", "202"]
    assert gateway.place_order_calls[0]["quantity"] == "0.12"
    assert gateway.place_order_calls[0]["reduceOnly"] == "true"
    close_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_BY_BOT")
    assert close_audit.details["reason"] == "aqrr_regime_flip"
    assert close_audit.details["lifecycle_reason"] == "aqrr_regime_flip"
    assert close_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"
    assert close_audit.details["aqrr_rejection_stage"] == "candidate_build"
    assert ws_manager.events[-1] == ("order_status_change", {"order_id": order.id, "status": "CLOSED_BY_BOT"})


@pytest.mark.asyncio
async def test_close_position_keeps_order_live_when_authoritative_close_quantity_is_inconclusive() -> None:
    gateway = FakeGateway(filters=make_filters())

    async def failing_positions(_credentials):
        raise RuntimeError("positions unavailable")

    gateway.positions = failing_positions
    notifier = FakeNotifier()
    ws_manager = CapturingWebSocketManager()
    manager = ApprovalOrderManager(gateway, ws_manager, notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.25"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
        strategy_context={
            "entry_filled_quantity": "0.358",
            "protection_quantity": "0.358",
        },
    )
    session = FakeSession(signal, order=order)

    closed = await manager.close_position(
        session,
        order_id=order.id,
        reason="aqrr_regime_flip",
        reason_context={
            "lifecycle_reason": "aqrr_regime_flip",
            "raw_aqrr_reason": "breakout_not_closed_through_level",
            "raw_aqrr_reasons": ["breakout_not_closed_through_level"],
        },
    )

    assert closed.status == OrderStatus.IN_POSITION
    assert gateway.place_order_calls == []
    assert gateway.place_algo_order_calls == []
    inconclusive_audit = next(
        item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_AUTHORITATIVE_RECOVERY_INCONCLUSIVE"
    )
    assert inconclusive_audit.details["authoritative_recovery_outcome"] == "authoritative_recovery_inconclusive"
    assert inconclusive_audit.details["lifecycle_reason"] == "authoritative_close_quantity_unconfirmed"
    assert inconclusive_audit.details["raw_aqrr_reason"] == "breakout_not_closed_through_level"


@pytest.mark.asyncio
async def test_close_position_prefers_authoritative_remote_quantity_over_stale_local_remaining_quantity() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "901", "avgPrice": "106.5"}],
        positions_payload=[
            {
                "symbol": "BCHUSDT",
                "positionAmt": "0.120",
                "entryPrice": "100.25",
                "markPrice": "106.5",
                "unRealizedProfit": "0",
            }
        ],
        account_trade_results={
            "901": [
                {
                    "qty": "0.120",
                    "price": "106.5",
                    "realizedPnl": "0.75",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    ws_manager = CapturingWebSocketManager()
    manager = ApprovalOrderManager(gateway, ws_manager, notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.25"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.358"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
        strategy_context={
            "entry_filled_quantity": "0.358",
            "protection_quantity": "0.358",
        },
    )
    session = FakeSession(signal, order=order)

    closed = await manager.close_position(
        session,
        order_id=order.id,
        reason="aqrr_regime_flip",
    )

    assert closed.status == OrderStatus.CLOSED_BY_BOT
    assert closed.close_type == "BOT"
    assert closed.remaining_quantity == Decimal("0")
    assert gateway.place_order_calls[0]["quantity"] == "0.12"
    assert gateway.place_order_calls[0]["reduceOnly"] == "true"


@pytest.mark.asyncio
async def test_close_position_uses_actual_partial_fill_quantity_after_remainder_cancelled() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "901", "avgPrice": "106.5"}],
        query_order_results={
            "101": {
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.120",
                "origQty": "0.358",
                "avgPrice": "100.25",
                "updateTime": now_ms,
            }
        },
        account_trade_results={
            "101": [
                {
                    "qty": "0.120",
                    "price": "100.25",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    ws_manager = CapturingWebSocketManager()
    manager = ApprovalOrderManager(gateway, ws_manager, notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        quantity=Decimal("0.358"),
        remaining_quantity=Decimal("0.120"),
        position_margin=Decimal("8.956265"),
        notional_value=Decimal("35.8"),
        rr_ratio=Decimal("3.0"),
        risk_budget_usdt=Decimal("8.0"),
        risk_usdt_at_stop=Decimal("1.8"),
        risk_pct_of_wallet=Decimal("18.0"),
        entry_order_id="101",
        tp_order_id="201",
        sl_order_id="202",
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
        strategy_context={
            "entry_filled_quantity": "0.120",
            "protection_quantity": "0.120",
        },
    )
    session = FakeSession(signal, order=order)

    closed = await manager.close_position(
        session,
        order_id=order.id,
        reason="aqrr_regime_flip",
    )

    assert closed.status == OrderStatus.CLOSED_BY_BOT
    assert closed.close_type == "BOT"
    assert closed.remaining_quantity == Decimal("0")
    assert closed.realized_pnl == Decimal("0.75")
    assert gateway.cancel_order_calls == [("BCHUSDT", "101")]
    assert gateway.place_order_calls[0]["quantity"] == "0.12"
    assert gateway.place_order_calls[0]["reduceOnly"] == "true"
    close_audit = next(item for item in session.added if isinstance(item, AuditLog) and item.event_type == "ORDER_CLOSED_BY_BOT")
    assert close_audit.details["reason"] == "aqrr_regime_flip"


@pytest.mark.asyncio
async def test_cancel_order_rejects_in_position_orders() -> None:
    gateway = FakeGateway(filters=make_filters())
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), FakeNotifier())
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
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
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="LEGACY_MODE",
    )
    session = FakeSession(signal, order=order)

    with pytest.raises(ValueError, match="Only pending entry orders can be cancelled"):
        await manager.cancel_order(session, order_id=order.id)

    assert gateway.cancel_order_calls == []
    assert gateway.cancel_algo_order_calls == []


@pytest.mark.asyncio
async def test_sync_order_triggers_stop_entry_from_algo_actual_order() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        place_algo_order_results=[
            {"algoId": "211"},
            {"algoId": "212"},
        ],
        query_algo_order_results={
            "101": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "301",
            },
            "212": {
                "algoStatus": "NEW",
                "actualOrderId": "",
            }
        },
        query_order_results={
            "301": {"status": "FILLED"},
        },
        account_trade_results={
            "301": [
                {
                    "qty": "0.358",
                    "price": "100.0",
                    "time": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
        leverage=4,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("95.1"),
        take_profit=Decimal("115.1"),
        entry_style="STOP_ENTRY",
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
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="AUTO_MODE",
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert gateway.query_algo_order_calls[0] == "101"
    assert ("BCHUSDT", "301") in gateway.query_order_calls
    assert notifier.messages[0]["message"] == "BCHUSDT position opened"


@pytest.mark.asyncio
async def test_sync_order_closes_position_when_algo_order_has_filled_actual_order() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_algo_order_results={
            "201": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "301",
            }
        },
        query_order_results={
            "301": {"status": "FILLED"},
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = Order(
        id=12,
        signal_id=signal.id,
        symbol=signal.symbol,
        direction=signal.direction,
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
        status=OrderStatus.IN_POSITION,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        approved_by="LEGACY_MODE",
        remaining_quantity=Decimal("0.358"),
    )
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.CLOSED_WIN
    assert synced.close_type == "TP"
    assert synced.close_price == Decimal("115.1")
    assert synced.realized_pnl == Decimal("5.4058")
    assert gateway.query_algo_order_calls == ["201"]
    assert gateway.query_order_calls == [("BCHUSDT", "101"), ("BCHUSDT", "301"), ("BCHUSDT", "101"), ("BCHUSDT", "101")]
    assert notifier.messages[0]["message"] == "BCHUSDT closed in profit"


@pytest.mark.asyncio
async def test_sync_order_marks_tp1_fill_and_keeps_partial_tp_order_open() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_algo_order_results={
            "201": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "301",
            }
        },
        query_order_results={
            "301": {"status": "FILLED"},
        },
        account_trade_results={
            "301": [
                {
                    "qty": "0.049",
                    "price": "107.4",
                    "realizedPnl": "0.3626",
                    "time": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = make_partial_order(signal=signal)
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.IN_POSITION
    assert synced.tp1_filled_at is not None
    assert synced.remaining_quantity == Decimal("0.050")
    assert synced.realized_pnl == Decimal("0.3626")
    assert synced.close_price == Decimal("107.4")
    assert any(isinstance(item, AuditLog) and item.event_type == "ORDER_PARTIAL_TP_FILLED" for item in session.added)
    assert notifier.messages[0]["message"] == "BCHUSDT locked in partial profit"


@pytest.mark.asyncio
async def test_sync_order_closes_partial_tp_trade_on_tp2_with_combined_profit() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_algo_order_results={
            "202": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "302",
            }
        },
        query_order_results={
            "302": {"status": "FILLED"},
        },
        account_trade_results={
            "302": [
                {
                    "qty": "0.050",
                    "price": "114.7",
                    "realizedPnl": "0.735",
                    "time": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = make_partial_order(signal=signal)
    order.tp1_filled_at = datetime.now(timezone.utc)
    order.remaining_quantity = Decimal("0.050")
    order.realized_pnl = Decimal("0.3626")
    order.close_price = Decimal("107.4")
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.CLOSED_WIN
    assert synced.close_type == "TP"
    assert synced.realized_pnl == Decimal("1.0976")
    assert synced.remaining_quantity == Decimal("0")
    assert notifier.messages[0]["message"] == "BCHUSDT closed in profit"


@pytest.mark.asyncio
async def test_sync_order_keeps_win_status_when_stop_loss_hits_after_tp1_profit() -> None:
    gateway = FakeGateway(
        filters=make_filters(),
        query_algo_order_results={
            "203": {
                "algoStatus": "TRIGGERED",
                "actualOrderId": "303",
            }
        },
        query_order_results={
            "303": {"status": "FILLED"},
        },
        account_trade_results={
            "303": [
                {
                    "qty": "0.050",
                    "price": "95.1",
                    "realizedPnl": "-0.245",
                    "time": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                }
            ]
        },
    )
    notifier = FakeNotifier()
    manager = ApprovalOrderManager(gateway, CapturingWebSocketManager(), notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    order = make_partial_order(signal=signal)
    order.tp1_filled_at = datetime.now(timezone.utc)
    order.remaining_quantity = Decimal("0.050")
    order.realized_pnl = Decimal("0.3626")
    order.close_price = Decimal("107.4")
    session = FakeSession(signal, order=order)

    synced = await manager.sync_order(session, order)

    assert synced.status == OrderStatus.CLOSED_WIN
    assert synced.close_type == "SL"
    assert synced.realized_pnl == Decimal("0.1176")
    assert notifier.messages[0]["message"] == "BCHUSDT closed in profit"


@pytest.mark.asyncio
async def test_close_position_submits_reduce_only_bot_exit_and_closes_order() -> None:
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    gateway = FakeGateway(
        filters=make_filters(),
        place_order_results=[{"orderId": "901", "avgPrice": "106.5"}],
        positions_payload=[
            {
                "symbol": "BCHUSDT",
                "positionAmt": "0.099",
                "entryPrice": "100.0",
                "markPrice": "106.5",
                "unRealizedProfit": "0",
            }
        ],
        account_trade_results={
            "901": [
                {
                    "qty": "0.099",
                    "price": "106.5",
                    "realizedPnl": "0.6435",
                    "time": now_ms,
                }
            ]
        },
    )
    notifier = FakeNotifier()
    ws_manager = CapturingWebSocketManager()
    manager = ApprovalOrderManager(gateway, ws_manager, notifier)
    signal = make_signal(
        direction=SignalDirection.LONG,
        entry_price=Decimal("100.07"),
        stop_loss=Decimal("95.02"),
        take_profit=Decimal("115.09"),
    )
    signal.extra_context = {
        "aqrr_raw_rejection_reason": "breakout_not_closed_through_level",
        "aqrr_raw_rejection_reasons": ["breakout_not_closed_through_level"],
        "aqrr_rejection_stage": "candidate_build",
        "setup_family": "breakout_retest",
        "entry_style": "LIMIT_GTD",
    }
    order = make_partial_order(signal=signal)
    session = FakeSession(signal, order=order)

    closed = await manager.close_position(
        session,
        order_id=order.id,
        reason="aqrr_regime_flip",
        reason_context={
            "lifecycle_reason": "aqrr_regime_flip",
            "raw_aqrr_reason": "breakout_not_closed_through_level",
            "raw_aqrr_reasons": ["breakout_not_closed_through_level"],
        },
    )

    assert closed.status == OrderStatus.CLOSED_BY_BOT
    assert closed.close_type == "BOT"
    assert closed.remaining_quantity == Decimal("0")
    assert gateway.cancel_algo_order_calls == ["201", "202", "203"]
    assert gateway.place_order_calls[0]["type"] == "MARKET"
    assert gateway.place_order_calls[0]["side"] == "SELL"
    assert gateway.place_order_calls[0]["reduceOnly"] == "true"
    assert any(
        isinstance(item, AuditLog)
        and item.event_type == "ORDER_CLOSED_BY_BOT"
        and item.details.get("reason") == "aqrr_regime_flip"
        and item.details.get("lifecycle_reason") == "aqrr_regime_flip"
        and item.details.get("raw_aqrr_reason") == "breakout_not_closed_through_level"
        and item.details.get("aqrr_rejection_stage") == "candidate_build"
        for item in session.added
    )
    assert ws_manager.events[-1] == ("order_status_change", {"order_id": order.id, "status": "CLOSED_BY_BOT"})
