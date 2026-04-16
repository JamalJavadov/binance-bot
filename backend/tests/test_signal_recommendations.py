from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.models.order  # noqa: F401
import app.models.scan_symbol_result  # noqa: F401
from app.models.enums import OrderStatus, ScanStatus, SignalDirection, SignalStatus, TriggerType
from app.models.scan_cycle import ScanCycle
from app.models.signal import Signal
from app.routers import signals as signals_router
from app.routers.signals import list_signal_recommendations
from app.services.binance_gateway import LeverageBracket, SymbolFilters
from app.services.order_manager import AccountSnapshot, OrderManager
from app.services.ws_manager import WebSocketManager


class FakeScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeExecuteResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return FakeScalarResult(self.rows)

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class RecommendationSession:
    def __init__(self, latest_completed_scan: ScanCycle | None, signals: list[Signal]) -> None:
        self.latest_completed_scan = latest_completed_scan
        self.signals = signals
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        if self.execute_calls == 1:
            rows = [] if self.latest_completed_scan is None else [self.latest_completed_scan]
            return FakeExecuteResult(rows)

        ranked = [
            signal
            for signal in self.signals
            if signal.scan_cycle_id == self.latest_completed_scan.id and signal.status == SignalStatus.QUALIFIED
        ]
        ranked.sort(
            key=lambda signal: (
                -(float(signal.rank_value) if signal.rank_value is not None else float(signal.final_score)),
                -signal.final_score,
                -signal.confirmation_score,
                signal.symbol,
            )
        )
        return FakeExecuteResult(ranked[:3])


class FakeGateway:
    def __init__(
        self,
        *,
        filters_by_symbol: dict[str, SymbolFilters],
        mark_prices_by_symbol: dict[str, str],
    ) -> None:
        self.filters_by_symbol = filters_by_symbol
        self.mark_prices_by_symbol = mark_prices_by_symbol
        self.mark_price_calls: list[str] = []
        self.exchange_info_calls = 0
        self.mark_prices_calls = 0
        self.leverage_brackets_calls = 0

    async def exchange_info(self) -> dict:
        self.exchange_info_calls += 1
        return {"symbols": [{"symbol": symbol} for symbol in self.filters_by_symbol]}

    async def read_cached_exchange_info(self) -> dict:
        return await self.exchange_info()

    def parse_symbol_filters(self, _exchange_info: dict) -> dict[str, SymbolFilters]:
        return self.filters_by_symbol

    async def mark_prices(self) -> dict[str, dict]:
        self.mark_prices_calls += 1
        return {
            symbol: {"markPrice": mark_price, "lastFundingRate": "0.0"}
            for symbol, mark_price in self.mark_prices_by_symbol.items()
        }

    async def read_cached_mark_prices(self) -> dict[str, dict]:
        return await self.mark_prices()

    async def mark_price(self, symbol: str) -> dict:
        self.mark_price_calls.append(symbol)
        return {"markPrice": self.mark_prices_by_symbol[symbol], "lastFundingRate": "0.0"}

    async def leverage_brackets(self, _credentials, symbol: str | None = None) -> dict[str, list[LeverageBracket]]:
        self.leverage_brackets_calls += 1
        symbols = [symbol] if symbol is not None else list(self.filters_by_symbol)
        return {
            item: [
                LeverageBracket(
                    bracket=1,
                    initial_leverage=10,
                    notional_cap=Decimal("100000"),
                    notional_floor=Decimal("0"),
                    maint_margin_ratio=Decimal("0.005"),
                    cum=Decimal("0"),
                )
            ]
            for item in symbols
        }

    async def read_cached_leverage_brackets(self, credentials) -> dict[str, list[LeverageBracket]]:
        return await self.leverage_brackets(credentials)


class FakeNotifier:
    async def send(self, **_payload) -> None:
        return None


class RecommendationOrderManager(OrderManager):
    def __init__(
        self,
        gateway: FakeGateway,
        *,
        credentials_available: bool = True,
        available_balance: str = "1000",
        active_orders: list[object] | None = None,
    ) -> None:
        super().__init__(gateway, WebSocketManager(), FakeNotifier())
        self.credentials_available = credentials_available
        self.available_balance = Decimal(available_balance)
        self.active_orders = list(active_orders or [])

    async def get_credentials(self, _session):
        if not self.credentials_available:
            return None
        return SimpleNamespace(api_key="key", private_key_pem="private")

    async def get_account_snapshot(self, _session, _credentials):
        return AccountSnapshot.from_available_balance(
            self.available_balance,
            reserve_fraction=self.BALANCE_RESERVE_FRACTION,
        )

    async def get_read_account_snapshot(self, session, credentials):
        return await self.get_account_snapshot(session, credentials)

    async def active_entry_orders(self, _session):
        return list(self.active_orders)


def build_signal(
    *,
    signal_id: int,
    symbol: str,
    scan_cycle_id: int,
    final_score: int,
    confirmation_score: int,
    rank_value: str | None = None,
    entry_price: str = "100",
    stop_loss: str = "95",
    take_profit: str = "115",
    status: SignalStatus = SignalStatus.QUALIFIED,
) -> Signal:
    now = datetime.now(timezone.utc)
    return Signal(
        id=signal_id,
        scan_cycle_id=scan_cycle_id,
        symbol=symbol,
        direction=SignalDirection.LONG,
        timeframe="4h",
        entry_price=Decimal(entry_price),
        stop_loss=Decimal(stop_loss),
        take_profit=Decimal(take_profit),
        rr_ratio=Decimal("3.0"),
        confirmation_score=confirmation_score,
        final_score=final_score,
        rank_value=Decimal(rank_value or str(final_score)),
        score_breakdown={"trend": final_score},
        reason_text=f"{symbol} qualified",
        swing_origin=Decimal("80"),
        swing_terminus=Decimal("120"),
        fib_0786_level=Decimal(entry_price),
        current_price_at_signal=Decimal(entry_price),
        expires_at=now + timedelta(hours=48),
        status=status,
        extra_context={},
        created_at=now,
        updated_at=now,
    )


def build_scan_cycle(*, scan_cycle_id: int, trigger_type: TriggerType) -> ScanCycle:
    now = datetime.now(timezone.utc)
    return ScanCycle(
        id=scan_cycle_id,
        started_at=now,
        completed_at=now,
        status=ScanStatus.COMPLETE,
        symbols_scanned=50,
        candidates_found=4,
        signals_qualified=3,
        trigger_type=trigger_type,
        progress_pct=100,
    )


def make_request(order_manager: RecommendationOrderManager, gateway: FakeGateway):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(order_manager=order_manager, gateway=gateway)))


@pytest.fixture(autouse=True)
def patch_settings_map(monkeypatch):
    async def fake_get_settings_map(_session) -> dict[str, str]:
        return {
            "risk_per_trade_pct": "2.0",
            "max_portfolio_risk_pct": "6.0",
            "max_leverage": "10",
            "deployable_equity_pct": "90",
            "max_book_spread_bps": "12",
            "min_24h_quote_volume_usdt": "25000000",
            "kill_switch_consecutive_stop_losses": "2",
            "kill_switch_daily_drawdown_pct": "4.0",
        }

    monkeypatch.setattr(signals_router, "get_settings_map", fake_get_settings_map)


def make_filters(
    symbol: str,
    *,
    step_size: str = "0.001",
    min_qty: str = "0.001",
    min_notional: str = "5",
) -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        tick_size=Decimal("0.1"),
        step_size=Decimal(step_size),
        min_qty=Decimal(min_qty),
        min_notional=Decimal(min_notional),
    )


@pytest.mark.asyncio
async def test_recommendations_endpoint_returns_top_three_ranked_signals_from_latest_scan() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    signals = [
        build_signal(signal_id=1, symbol="CCCUSDT", scan_cycle_id=latest_scan_id, final_score=75, confirmation_score=55),
        build_signal(signal_id=2, symbol="AAAUSDT", scan_cycle_id=latest_scan_id, final_score=92, confirmation_score=65),
        build_signal(signal_id=3, symbol="BBBUSDT", scan_cycle_id=latest_scan_id, final_score=92, confirmation_score=70),
        build_signal(signal_id=4, symbol="DDDUSDT", scan_cycle_id=latest_scan_id, final_score=88, confirmation_score=60),
        build_signal(signal_id=5, symbol="EEEUSDT", scan_cycle_id=latest_scan_id, final_score=70, confirmation_score=50),
        build_signal(
            signal_id=6,
            symbol="OLDUSDT",
            scan_cycle_id=latest_scan_id - 1,
            final_score=99,
            confirmation_score=99,
        ),
        build_signal(
            signal_id=7,
            symbol="APPROVEDUSDT",
            scan_cycle_id=latest_scan_id,
            final_score=95,
            confirmation_score=90,
            status=SignalStatus.APPROVED,
        ),
    ]
    filters = {signal.symbol: make_filters(signal.symbol) for signal in signals}
    mark_prices = {signal.symbol: "101" for signal in signals}
    gateway = FakeGateway(filters_by_symbol=filters, mark_prices_by_symbol=mark_prices)
    order_manager = RecommendationOrderManager(gateway)
    session = RecommendationSession(latest_scan, signals)

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert response.latest_completed_scan_id == latest_scan_id
    assert response.latest_completed_scan_trigger_type == TriggerType.AUTO_MODE
    assert response.latest_completed_scan_strategy_label == "AQRR Binance USD-M Strategy"
    assert [item.rank for item in response.items] == [1, 2, 3]
    assert [item.signal.symbol for item in response.items] == ["BBBUSDT", "AAAUSDT", "DDDUSDT"]
    assert all(item.signal.scan_trigger_type == TriggerType.AUTO_MODE for item in response.items)
    assert all(item.live_readiness.can_open_now is True for item in response.items)


@pytest.mark.asyncio
async def test_recommendations_endpoint_prefers_rank_value_over_final_score() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    signals = [
        build_signal(
            signal_id=1,
            symbol="LOWERFINALUSDT",
            scan_cycle_id=latest_scan_id,
            final_score=88,
            confirmation_score=60,
            rank_value="95",
        ),
        build_signal(
            signal_id=2,
            symbol="HIGHFINALUSDT",
            scan_cycle_id=latest_scan_id,
            final_score=92,
            confirmation_score=70,
            rank_value="92",
        ),
        build_signal(
            signal_id=3,
            symbol="THIRDUSDT",
            scan_cycle_id=latest_scan_id,
            final_score=85,
            confirmation_score=50,
            rank_value="85",
        ),
    ]
    gateway = FakeGateway(
        filters_by_symbol={signal.symbol: make_filters(signal.symbol) for signal in signals},
        mark_prices_by_symbol={signal.symbol: "101" for signal in signals},
    )
    order_manager = RecommendationOrderManager(gateway)
    session = RecommendationSession(latest_scan, signals)

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert [item.signal.symbol for item in response.items] == ["LOWERFINALUSDT", "HIGHFINALUSDT", "THIRDUSDT"]


@pytest.mark.asyncio
async def test_recommendations_endpoint_skips_binance_reads_when_latest_scan_has_no_qualified_signals() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    signals = [
        build_signal(
            signal_id=1,
            symbol="AAAUSDT",
            scan_cycle_id=latest_scan_id,
            final_score=92,
            confirmation_score=65,
            status=SignalStatus.APPROVED,
        ),
        build_signal(
            signal_id=2,
            symbol="BBBUSDT",
            scan_cycle_id=latest_scan_id,
            final_score=90,
            confirmation_score=60,
            status=SignalStatus.CANDIDATE,
        ),
    ]
    gateway = FakeGateway(
        filters_by_symbol={signal.symbol: make_filters(signal.symbol) for signal in signals},
        mark_prices_by_symbol={signal.symbol: "101" for signal in signals},
    )
    order_manager = RecommendationOrderManager(gateway)
    session = RecommendationSession(latest_scan, signals)

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert response.latest_completed_scan_id == latest_scan_id
    assert response.items == []
    assert gateway.exchange_info_calls == 0
    assert gateway.mark_prices_calls == 0
    assert gateway.leverage_brackets_calls == 0


@pytest.mark.asyncio
async def test_recommendations_endpoint_marks_affordable_and_resized_signals_green() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    affordable_signal = build_signal(
        signal_id=1,
        symbol="SAFEUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=90,
        confirmation_score=70,
        entry_price="100",
        stop_loss="60",
        take_profit="220",
    )
    resized_signal = build_signal(
        signal_id=2,
        symbol="RESIZEUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=80,
        confirmation_score=65,
        entry_price="100",
        stop_loss="95",
        take_profit="115",
    )
    gateway = FakeGateway(
        filters_by_symbol={
            "SAFEUSDT": make_filters("SAFEUSDT"),
            "RESIZEUSDT": make_filters("RESIZEUSDT"),
        },
        mark_prices_by_symbol={
            "SAFEUSDT": "101",
            "RESIZEUSDT": "101",
        },
    )
    order_manager = RecommendationOrderManager(gateway)
    session = RecommendationSession(latest_scan, [affordable_signal, resized_signal])

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert [item.signal.symbol for item in response.items] == ["SAFEUSDT", "RESIZEUSDT"]
    assert response.items[0].live_readiness.can_open_now is True
    assert response.items[0].live_readiness.order_preview is not None
    assert response.items[0].live_readiness.order_preview.status == "affordable"
    assert response.items[1].live_readiness.can_open_now is True
    assert response.items[1].live_readiness.order_preview is not None
    assert response.items[1].live_readiness.order_preview.status == "resized_to_budget"
    assert response.items[1].live_readiness.order_preview.auto_resized is True


@pytest.mark.asyncio
async def test_recommendations_endpoint_marks_red_for_missing_credentials() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    signal = build_signal(
        signal_id=1,
        symbol="NOCREDUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=90,
        confirmation_score=70,
    )
    gateway = FakeGateway(
        filters_by_symbol={"NOCREDUSDT": make_filters("NOCREDUSDT")},
        mark_prices_by_symbol={"NOCREDUSDT": "100"},
    )
    order_manager = RecommendationOrderManager(gateway, credentials_available=False)
    session = RecommendationSession(latest_scan, [signal])

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert len(response.items) == 1
    assert response.items[0].live_readiness.can_open_now is False
    assert response.items[0].live_readiness.failure_reason == "API credentials are required before placing live orders."


@pytest.mark.asyncio
async def test_recommendations_endpoint_marks_red_for_insufficient_balance() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    signal = build_signal(
        signal_id=1,
        symbol="LOWBALUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=95,
        confirmation_score=70,
    )
    gateway = FakeGateway(
        filters_by_symbol={"LOWBALUSDT": make_filters("LOWBALUSDT", step_size="0.1")},
        mark_prices_by_symbol={"LOWBALUSDT": "101"},
    )
    order_manager = RecommendationOrderManager(gateway, available_balance="1")
    session = RecommendationSession(latest_scan, [signal])

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert len(response.items) == 1
    assert response.items[0].live_readiness.can_open_now is False
    assert response.items[0].live_readiness.order_preview is not None
    assert response.items[0].live_readiness.order_preview.status == "too_small_for_exchange"
    assert "slot budget is too small to reach Binance minimum order size" in (response.items[0].live_readiness.failure_reason or "")


@pytest.mark.asyncio
async def test_recommendations_endpoint_blocks_when_shared_entry_slots_are_full() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    signal = build_signal(
        signal_id=1,
        symbol="FULLUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=95,
        confirmation_score=70,
    )
    gateway = FakeGateway(
        filters_by_symbol={"FULLUSDT": make_filters("FULLUSDT")},
        mark_prices_by_symbol={"FULLUSDT": "101"},
    )
    active_orders = [
        SimpleNamespace(status=OrderStatus.ORDER_PLACED),
        SimpleNamespace(status=OrderStatus.IN_POSITION),
        SimpleNamespace(status=OrderStatus.ORDER_PLACED),
    ]
    order_manager = RecommendationOrderManager(gateway, active_orders=active_orders)
    session = RecommendationSession(latest_scan, [signal])

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert len(response.items) == 1
    assert response.items[0].live_readiness.can_open_now is False
    assert response.items[0].live_readiness.failure_reason == "All 3 shared entry slots are already in use by pending entry orders or open positions."
    assert response.items[0].live_readiness.order_preview is not None
    assert response.items[0].live_readiness.order_preview.can_place is False
    assert response.items[0].live_readiness.order_preview.reason == response.items[0].live_readiness.failure_reason


@pytest.mark.asyncio
async def test_recommendations_endpoint_blocks_when_symbol_already_has_active_entry_order() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    signal = build_signal(
        signal_id=1,
        symbol="FULLUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=95,
        confirmation_score=70,
    )
    gateway = FakeGateway(
        filters_by_symbol={"FULLUSDT": make_filters("FULLUSDT")},
        mark_prices_by_symbol={"FULLUSDT": "101"},
    )
    active_orders = [
        SimpleNamespace(symbol="FULLUSDT", status=OrderStatus.ORDER_PLACED),
    ]
    order_manager = RecommendationOrderManager(gateway, active_orders=active_orders)
    session = RecommendationSession(latest_scan, [signal])

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert len(response.items) == 1
    assert response.items[0].live_readiness.can_open_now is False
    assert response.items[0].live_readiness.failure_reason == "FULLUSDT already has an active entry order. Only one shared entry slot is allowed per coin."
    assert response.items[0].live_readiness.order_preview is not None
    assert response.items[0].live_readiness.order_preview.can_place is False
    assert response.items[0].live_readiness.order_preview.reason == response.items[0].live_readiness.failure_reason


@pytest.mark.asyncio
async def test_recommendations_endpoint_marks_red_for_exchange_minimum_and_stale_failures() -> None:
    latest_scan_id = 7
    latest_scan = build_scan_cycle(scan_cycle_id=latest_scan_id, trigger_type=TriggerType.AUTO_MODE)
    too_small_signal = build_signal(
        signal_id=2,
        symbol="MINONLYUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=90,
        confirmation_score=68,
    )
    stale_signal = build_signal(
        signal_id=3,
        symbol="STALEUSDT",
        scan_cycle_id=latest_scan_id,
        final_score=85,
        confirmation_score=64,
    )
    gateway = FakeGateway(
        filters_by_symbol={
            "LOWBALUSDT": make_filters("LOWBALUSDT"),
            "MINONLYUSDT": make_filters("MINONLYUSDT", min_notional="5000"),
            "STALEUSDT": make_filters("STALEUSDT"),
        },
        mark_prices_by_symbol={
            "MINONLYUSDT": "101",
            "STALEUSDT": "99",
        },
    )
    order_manager = RecommendationOrderManager(gateway, available_balance="1000")
    session = RecommendationSession(latest_scan, [too_small_signal, stale_signal])

    response = await list_signal_recommendations(make_request(order_manager, gateway), session)

    assert [item.signal.symbol for item in response.items] == ["MINONLYUSDT", "STALEUSDT"]
    assert response.items[0].live_readiness.can_open_now is False
    assert response.items[0].live_readiness.order_preview is not None
    assert response.items[0].live_readiness.order_preview.status == "too_small_for_exchange"
    assert "below Binance minimum notional" in (response.items[0].live_readiness.failure_reason or "")
    assert response.items[1].live_readiness.can_open_now is False
    assert response.items[1].live_readiness.failure_reason is not None
    assert "entry level has already been crossed" in response.items[1].live_readiness.failure_reason
