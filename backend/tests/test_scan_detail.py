from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.aqrr_trade_stat import AqrrTradeStat
from app.models.audit_log import AuditLog
from app.models.enums import AuditLevel, OrderStatus, ScanStatus, ScanSymbolOutcome, SignalDirection, SignalStatus, TriggerType
from app.models.scan_cycle import ScanCycle
from app.models.scan_symbol_result import ScanSymbolResult
from app.models.signal import Signal
from app.routers.scan import scan_cycle_detail
from app.services.binance_gateway import SymbolFilters
from app.services.order_manager import AccountSnapshot, OrderManager, SharedEntrySlotBudget
from app.services.scanner import ScannerService
from app.services.strategy.aqrr import AqrrEvaluation, SelectionDecision
from app.services.strategy.indicators import required_15m_candles_for_volatility_shock
from app.services.strategy.types import SetupCandidate
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


class FakeSession:
    def __init__(self):
        self.added: list[object] = []
        self._ids: dict[type, int] = {}

    def add(self, obj):
        obj_type = type(obj)
        next_id = self._ids.get(obj_type, 0) + 1
        self._ids[obj_type] = next_id
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            setattr(obj, "id", next_id)
        self.added.append(obj)

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None

    async def flush(self):
        return None

    async def execute(self, _statement):
        return FakeExecuteResult([])

    async def get(self, _model, _id):
        return None


class DetailSession:
    def __init__(self, cycle: ScanCycle | None, *, results: list[ScanSymbolResult], workflow: list[AuditLog]):
        self.cycle = cycle
        self._responses = [results, workflow]

    async def get(self, _model, _id):
        return self.cycle

    async def execute(self, _statement):
        return FakeExecuteResult(self._responses.pop(0))


class DummyGateway:
    def __init__(self) -> None:
        self.klines_calls: list[tuple[str, str, int]] = []

    async def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                },
                {
                    "symbol": "ETHUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                },
            ]
        }

    def parse_symbol_filters(self, _exchange_info):
        filters = SymbolFilters(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        eth_filters = SymbolFilters(
            symbol="ETHUSDT",
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
        return {"BTCUSDT": filters, "ETHUSDT": eth_filters}

    async def leverage_brackets(self, _credentials, symbol: str | None = None):
        return {}

    async def ticker_24hr(self):
        return [
            {"symbol": "BTCUSDT", "quoteVolume": "50000000"},
            {"symbol": "ETHUSDT", "quoteVolume": "45000000"},
        ]

    async def book_tickers(self):
        return {
            "BTCUSDT": {"bidPrice": "100.9", "askPrice": "101.0", "bidQty": "50", "askQty": "50"},
            "ETHUSDT": {"bidPrice": "100.9", "askPrice": "101.0", "bidQty": "50", "askQty": "50"},
        }

    async def mark_prices(self):
        return {
            "BTCUSDT": {"markPrice": "101.0", "lastFundingRate": "0.0"},
            "ETHUSDT": {"markPrice": "101.0", "lastFundingRate": "0.0"},
        }

    async def mark_price(self, symbol: str):
        return {"markPrice": "101.0", "lastFundingRate": "0.0", "symbol": symbol}

    async def klines(self, symbol: str, interval: str, limit: int):
        self.klines_calls.append((symbol, interval, limit))
        return [
            [index, "100.0", "101.0", "99.0", "100.5", "1000", index + 1, "0", "0", "0", "0", True]
            for index in range(limit)
        ]


class DummyNotifier:
    async def send(self, **_payload):
        return None


class DummyOrderManager(OrderManager):
    def __init__(self) -> None:
        super().__init__(gateway=None, ws_manager=WebSocketManager(), notifier=DummyNotifier())

    async def get_credentials(self, _session):
        return None

    async def get_account_snapshot(self, _session, _credentials):
        return AccountSnapshot.from_available_balance(
            Decimal("1000"),
            reserve_fraction=OrderManager.BALANCE_RESERVE_FRACTION,
        )

    async def get_shared_entry_slot_budget(self, _session, *, account_snapshot=None):
        return SharedEntrySlotBudget(
            slot_cap=3,
            active_entry_order_count=0,
            remaining_entry_slots=3,
            active_symbols=frozenset(),
            deployable_equity=Decimal("900"),
            committed_initial_margin=Decimal("0"),
            remaining_deployable_equity=Decimal("900"),
            portfolio_budget=Decimal("900"),
            per_slot_budget=Decimal("300"),
        )

    def build_preview(self, **_kwargs):
        return {
            "status": "affordable",
            "can_place": True,
            "auto_resized": False,
            "requested_quantity": "1",
            "final_quantity": "1",
            "max_affordable_quantity": "1",
            "mark_price_used": "101",
            "entry_notional": "100",
            "required_initial_margin": "20",
            "estimated_entry_fee": "0.1",
            "available_balance": "1000",
            "reserve_balance": "100",
            "usable_balance": "900",
            "risk_budget_usdt": "300",
            "risk_usdt_at_stop": "10",
            "recommended_leverage": 5,
            "reason": None,
        }


class StubMarketHealth:
    def __init__(self, snapshots: dict[str, dict[str, object]]) -> None:
        self.snapshots = snapshots

    async def snapshot(self, symbol: str, *, fallback_book_ticker=None, fallback_mark_price=None):
        payload = self.snapshots.get(symbol.upper(), {})
        return type(
            "Snapshot",
            (),
            {
                "book_ticker": payload.get("book_ticker", fallback_book_ticker),
                "mark_price": payload.get("mark_price", fallback_mark_price),
                "spread_bps": payload.get("spread_bps"),
                "spread_median_bps": payload.get("spread_median_bps"),
                "spread_relative_ratio": payload.get("spread_relative_ratio"),
                "relative_spread_ready": payload.get("relative_spread_ready", False),
                "relative_spread_sample_count": payload.get("relative_spread_sample_count", 0),
                "book_stable": payload.get("book_stable", True),
            },
        )()


def _candidate(*, symbol: str, final_score: int, rank_value: float, family: str) -> SetupCandidate:
    return SetupCandidate(
        symbol=symbol,
        direction=SignalDirection.LONG,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=117.0,
        actual_rr=3.4,
        net_r_multiple=3.4,
        estimated_cost=0.12,
        confirmation_score=80,
        final_score=final_score,
        rank_value=rank_value,
        setup_family=family,
        setup_variant=f"{family}_variant",
        entry_style="LIMIT_GTD",
        market_state="BULL_TREND",
        execution_tier="TIER_A",
        score_breakdown={
            "structure_quality": 22,
            "regime_alignment": 18,
            "liquidity_execution_quality": 14,
            "reward_headroom_quality": 9,
        },
        reason_text=f"{symbol} aqrr candidate",
        current_price=101.0,
        swing_origin=95.0,
        swing_terminus=110.0,
    )


@pytest.mark.asyncio
async def test_scanner_persists_aqrr_metadata_and_selection_outcomes(monkeypatch) -> None:
    import app.services.scanner as scanner_module

    btc_candidate = _candidate(symbol="BTCUSDT", final_score=92, rank_value=92.0, family="breakout_retest")
    eth_candidate = _candidate(symbol="ETHUSDT", final_score=88, rank_value=88.0, family="pullback_continuation")

    async def fake_get_settings_map(_session):
        return {
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

    def fake_evaluate_symbol(*, symbol: str, **_kwargs):
        candidate = btc_candidate if symbol == "BTCUSDT" else eth_candidate
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.CANDIDATE,
            direction=SignalDirection.LONG,
            candidates=[candidate],
            reason_text="aqrr_candidate_ready",
            filter_reasons=[],
            diagnostic={"market_state": "BULL_TREND", "execution_tier": "TIER_A"},
        )

    def fake_select_candidates(candidates: list[SetupCandidate], **_kwargs):
        rejected = {
            ("ETHUSDT", "LONG", round(eth_candidate.entry_price, 8)): "correlation_conflict",
        }
        return SelectionDecision(selected=[btc_candidate], rejected=rejected)

    monkeypatch.setattr(scanner_module, "get_settings_map", fake_get_settings_map)
    monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate_symbol)
    monkeypatch.setattr(scanner_module, "select_candidates", fake_select_candidates)

    session = FakeSession()
    service = ScannerService(DummyGateway(), WebSocketManager(), DummyOrderManager(), DummyNotifier())

    cycle = await service.run_scan(session, trigger_type=TriggerType.AUTO_MODE)

    results = [item for item in session.added if isinstance(item, ScanSymbolResult)]
    signals = [item for item in session.added if isinstance(item, Signal)]

    assert cycle.status == ScanStatus.COMPLETE
    assert cycle.signals_qualified == 1
    assert len(results) == 2
    assert len(signals) == 2

    btc_result = next(item for item in results if item.symbol == "BTCUSDT")
    eth_result = next(item for item in results if item.symbol == "ETHUSDT")
    btc_signal = next(item for item in signals if item.symbol == "BTCUSDT")
    eth_signal = next(item for item in signals if item.symbol == "ETHUSDT")

    assert btc_result.outcome == ScanSymbolOutcome.QUALIFIED
    assert eth_result.outcome == ScanSymbolOutcome.CANDIDATE
    assert eth_result.extra_context["selection_rejection_reason"] == "correlation_conflict"

    assert btc_signal.status == SignalStatus.QUALIFIED
    assert eth_signal.status == SignalStatus.CANDIDATE
    assert btc_signal.timeframe == "15m"
    assert btc_signal.fib_0786_level is None
    assert btc_signal.extra_context["strategy_key"] == "aqrr_binance_usdm"
    assert btc_signal.extra_context["strategy_label"] == "AQRR Binance USD-M Strategy"
    assert btc_signal.extra_context["market_state"] == "BULL_TREND"
    assert btc_signal.extra_context["setup_family"] == "breakout_retest"


@pytest.mark.asyncio
async def test_scanner_uses_30_day_15m_window_for_volatility_shock_inputs(monkeypatch) -> None:
    import app.services.scanner as scanner_module

    async def fake_get_settings_map(_session):
        return {
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

    def fake_evaluate_symbol(*, symbol: str, **_kwargs):
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.NO_SETUP,
            direction=None,
            candidates=[],
            reason_text=f"{symbol} no setup",
            filter_reasons=["no_aqrr_setup"],
            diagnostic={"market_state": "BULL_TREND", "execution_tier": "TIER_A"},
        )

    monkeypatch.setattr(scanner_module, "get_settings_map", fake_get_settings_map)
    monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate_symbol)

    session = FakeSession()
    gateway = DummyGateway()
    service = ScannerService(gateway, WebSocketManager(), DummyOrderManager(), DummyNotifier())

    await service.run_scan(session, trigger_type=TriggerType.AUTO_MODE)

    expected_limit = required_15m_candles_for_volatility_shock(atr_period=14)
    fifteen_minute_limits = [limit for _, interval, limit in gateway.klines_calls if interval == "15m"]
    assert fifteen_minute_limits
    assert all(limit == expected_limit for limit in fifteen_minute_limits)


@pytest.mark.asyncio
async def test_scanner_scans_all_eligible_symbols_and_keeps_priority_symbols_first(monkeypatch) -> None:
    import app.services.scanner as scanner_module

    scanned_symbols: list[str] = []

    class UniverseGateway(DummyGateway):
        async def exchange_info(self):
            return {
                "symbols": [
                    {"symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT"},
                    {"symbol": "BBBUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT"},
                    {"symbol": "CCCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT"},
                    {"symbol": "PRIORITYUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT"},
                    {"symbol": "DDDUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT"},
                ]
            }

        def parse_symbol_filters(self, _exchange_info):
            filters = SymbolFilters(
                symbol="AAAUSDT",
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                min_notional=Decimal("5"),
            )
            return {
                "AAAUSDT": filters,
                "BBBUSDT": SymbolFilters(symbol="BBBUSDT", tick_size=Decimal("0.1"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional=Decimal("5")),
                "CCCUSDT": SymbolFilters(symbol="CCCUSDT", tick_size=Decimal("0.1"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional=Decimal("5")),
                "PRIORITYUSDT": SymbolFilters(symbol="PRIORITYUSDT", tick_size=Decimal("0.1"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional=Decimal("5")),
                "DDDUSDT": SymbolFilters(symbol="DDDUSDT", tick_size=Decimal("0.1"), step_size=Decimal("0.001"), min_qty=Decimal("0.001"), min_notional=Decimal("5")),
            }

        async def ticker_24hr(self):
            return [
                {"symbol": "AAAUSDT", "quoteVolume": "400000000"},
                {"symbol": "BBBUSDT", "quoteVolume": "300000000"},
                {"symbol": "CCCUSDT", "quoteVolume": "250000000"},
                {"symbol": "PRIORITYUSDT", "quoteVolume": "200000000"},
                {"symbol": "DDDUSDT", "quoteVolume": "100000000"},
            ]

        async def book_tickers(self):
            return {
                "AAAUSDT": {"bidPrice": "100.9", "askPrice": "101.0", "bidQty": "50", "askQty": "50"},
                "BBBUSDT": {"bidPrice": "100.9", "askPrice": "101.0", "bidQty": "50", "askQty": "50"},
                "CCCUSDT": {"bidPrice": "100.9", "askPrice": "101.0", "bidQty": "50", "askQty": "50"},
                "PRIORITYUSDT": {"bidPrice": "100.9", "askPrice": "101.0", "bidQty": "50", "askQty": "50"},
                "DDDUSDT": {"bidPrice": "100.9", "askPrice": "101.0", "bidQty": "50", "askQty": "50"},
            }

        async def mark_prices(self):
            return {
                "AAAUSDT": {"markPrice": "101.0", "lastFundingRate": "0.0"},
                "BBBUSDT": {"markPrice": "101.0", "lastFundingRate": "0.0"},
                "CCCUSDT": {"markPrice": "101.0", "lastFundingRate": "0.0"},
                "PRIORITYUSDT": {"markPrice": "101.0", "lastFundingRate": "0.0"},
                "DDDUSDT": {"markPrice": "101.0", "lastFundingRate": "0.0"},
            }

    async def fake_get_settings_map(_session):
        return {
            "risk_per_trade_pct": "2.0",
            "max_portfolio_risk_pct": "6.0",
            "max_leverage": "10",
            "deployable_equity_pct": "90",
            "max_book_spread_bps": "12",
            "min_24h_quote_volume_usdt": "0",
            "kill_switch_consecutive_stop_losses": "2",
            "kill_switch_daily_drawdown_pct": "4.0",
            "auto_mode_max_entry_drift_pct": "5.0",
        }

    def fake_evaluate_symbol(*, symbol: str, **_kwargs):
        scanned_symbols.append(symbol)
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.NO_SETUP,
            direction=None,
            candidates=[],
            reason_text="no_aqrr_setup",
            filter_reasons=["no_aqrr_setup"],
            diagnostic={"market_state": "BULL_TREND", "execution_tier": "TIER_A"},
        )

    monkeypatch.setattr(scanner_module, "get_settings_map", fake_get_settings_map)
    monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate_symbol)

    session = FakeSession()
    service = ScannerService(UniverseGateway(), WebSocketManager(), DummyOrderManager(), DummyNotifier())

    cycle = await service.run_scan(session, trigger_type=TriggerType.AUTO_MODE, priority_symbols=["PRIORITYUSDT"])
    results = [item for item in session.added if isinstance(item, ScanSymbolResult)]

    assert cycle.symbols_scanned == 5
    assert scanned_symbols == ["PRIORITYUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert [item.symbol for item in results] == ["PRIORITYUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
    ddd_result = next(item for item in results if item.symbol == "DDDUSDT")
    assert ddd_result.outcome == ScanSymbolOutcome.FILTERED_OUT
    assert ddd_result.filter_reasons == ["quote_volume_below_liquidity_floor"]


@pytest.mark.asyncio
async def test_scanner_rejects_relative_spread_after_market_health_warmup(monkeypatch) -> None:
    import app.services.scanner as scanner_module

    async def fake_get_settings_map(_session):
        return {
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

    monkeypatch.setattr(scanner_module, "get_settings_map", fake_get_settings_map)
    session = FakeSession()
    service = ScannerService(
        DummyGateway(),
        WebSocketManager(),
        DummyOrderManager(),
        DummyNotifier(),
        market_health=StubMarketHealth(
            {
                "BTCUSDT": {
                    "book_ticker": {"bidPrice": "100.0", "askPrice": "100.1", "bidQty": "10", "askQty": "10"},
                    "mark_price": {"markPrice": "100.0", "lastFundingRate": "0.0"},
                    "spread_bps": 9.0,
                    "spread_median_bps": 3.0,
                    "spread_relative_ratio": 3.0,
                    "relative_spread_ready": True,
                    "relative_spread_sample_count": 120,
                    "book_stable": True,
                },
                "ETHUSDT": {
                    "book_ticker": {"bidPrice": "100.0", "askPrice": "100.1", "bidQty": "10", "askQty": "10"},
                    "mark_price": {"markPrice": "100.0", "lastFundingRate": "0.0"},
                    "spread_bps": 9.0,
                    "spread_median_bps": 3.5,
                    "spread_relative_ratio": 2.57,
                    "relative_spread_ready": True,
                    "relative_spread_sample_count": 120,
                    "book_stable": True,
                },
            }
        ),
    )

    cycle = await service.run_scan(session, trigger_type=TriggerType.AUTO_MODE)
    results = [item for item in session.added if isinstance(item, ScanSymbolResult)]

    assert cycle.status == ScanStatus.COMPLETE
    assert all(result.filter_reasons == ["spread_relative_above_threshold"] for result in results)


@pytest.mark.asyncio
async def test_scanner_allows_absolute_spread_when_relative_filter_is_not_warmed(monkeypatch) -> None:
    import app.services.scanner as scanner_module

    async def fake_get_settings_map(_session):
        return {
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

    def fake_evaluate_symbol(*, symbol: str, **_kwargs):
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.NO_SETUP,
            direction=None,
            candidates=[],
            reason_text="no_aqrr_setup",
            filter_reasons=["no_aqrr_setup"],
            diagnostic={"market_state": "BULL_TREND", "execution_tier": "TIER_A"},
        )

    monkeypatch.setattr(scanner_module, "get_settings_map", fake_get_settings_map)
    monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate_symbol)
    session = FakeSession()
    service = ScannerService(
        DummyGateway(),
        WebSocketManager(),
        DummyOrderManager(),
        DummyNotifier(),
        market_health=StubMarketHealth(
            {
                "BTCUSDT": {
                    "book_ticker": {"bidPrice": "100.0", "askPrice": "100.1", "bidQty": "10", "askQty": "10"},
                    "mark_price": {"markPrice": "100.0", "lastFundingRate": "0.0"},
                    "spread_bps": 9.0,
                    "spread_median_bps": 3.0,
                    "spread_relative_ratio": None,
                    "relative_spread_ready": False,
                    "relative_spread_sample_count": 20,
                    "book_stable": True,
                },
                "ETHUSDT": {
                    "book_ticker": {"bidPrice": "100.0", "askPrice": "100.1", "bidQty": "10", "askQty": "10"},
                    "mark_price": {"markPrice": "100.0", "lastFundingRate": "0.0"},
                    "spread_bps": 9.0,
                    "spread_median_bps": 3.0,
                    "spread_relative_ratio": None,
                    "relative_spread_ready": False,
                    "relative_spread_sample_count": 20,
                    "book_stable": True,
                },
            }
        ),
    )

    await service.run_scan(session, trigger_type=TriggerType.AUTO_MODE)
    results = [item for item in session.added if isinstance(item, ScanSymbolResult)]

    assert all(result.filter_reasons == ["no_aqrr_setup"] for result in results)
    assert all(result.extra_context["relative_spread_filter_active"] is False for result in results)


def test_scanner_candidate_with_stats_falls_back_to_final_score_without_sufficient_history() -> None:
    candidate = _candidate(symbol="BTCUSDT", final_score=84, rank_value=84.0, family="breakout_retest")

    enriched = ScannerService._candidate_with_stats(
        candidate,
        atr_percentile=0.45,
        stats_map={},
    )

    assert enriched.rank_value == 84.0
    assert enriched.extra_context["rank_method"] == "final_score"
    assert enriched.extra_context["calibrated_hit_rate_score"] is None
    assert enriched.extra_context["score_band"] == "80_89"
    assert enriched.extra_context["volatility_band"] == "normal"


def test_scanner_candidate_with_stats_uses_calibrated_hit_rate_when_bucket_is_ready() -> None:
    candidate = _candidate(symbol="BTCUSDT", final_score=80, rank_value=80.0, family="breakout_retest")
    stat = AqrrTradeStat(
        bucket_key="breakout_retest|LONG|BULL_TREND|80_89|normal|TIER_A",
        setup_family="breakout_retest",
        direction=SignalDirection.LONG,
        market_state="BULL_TREND",
        score_band="80_89",
        volatility_band="normal",
        execution_tier="TIER_A",
        closed_trade_count=20,
        win_count=15,
        loss_count=5,
    )

    enriched = ScannerService._candidate_with_stats(
        candidate,
        atr_percentile=0.45,
        stats_map={stat.bucket_key: stat},
    )

    assert enriched.rank_value == pytest.approx(78.5)
    assert enriched.extra_context["rank_method"] == "calibrated_hit_rate"
    assert enriched.extra_context["calibrated_hit_rate_score"] == 75.0
    assert enriched.extra_context["stats_bucket_key"] == stat.bucket_key


def test_liquidity_floor_diagnostics_uses_percentile_floor_instead_of_harder_configured_min() -> None:
    diagnostics = ScannerService._liquidity_floor_diagnostics(
        config=type("Config", (), {"min_24h_quote_volume_usdt": 50_000_000})(),
        eligible_symbols=["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT"],
        ticker_map={
            "AAAUSDT": {"quoteVolume": "20000000"},
            "BBBUSDT": {"quoteVolume": "30000000"},
            "CCCUSDT": {"quoteVolume": "40000000"},
            "DDDUSDT": {"quoteVolume": "50000000"},
            "EEEUSDT": {"quoteVolume": "60000000"},
        },
    )

    assert diagnostics["liquidity_floor_method"] == "percentile_30"
    assert diagnostics["liquidity_floor_percentile_30"] == 30_000_000.0
    assert diagnostics["effective_liquidity_floor"] == 30_000_000.0
    assert diagnostics["configured_min_24h_quote_volume_usdt"] == 50_000_000.0


def test_liquidity_floor_diagnostics_falls_back_to_25m_when_percentile_unavailable() -> None:
    diagnostics = ScannerService._liquidity_floor_diagnostics(
        config=type("Config", (), {"min_24h_quote_volume_usdt": 50_000_000})(),
        eligible_symbols=["AAAUSDT", "BBBUSDT"],
        ticker_map={
            "AAAUSDT": {"quoteVolume": "0"},
            "BBBUSDT": {"quoteVolume": "0"},
        },
    )

    assert diagnostics["liquidity_floor_method"] == "fallback_25m"
    assert diagnostics["liquidity_floor_percentile_30"] is None
    assert diagnostics["effective_liquidity_floor"] == 25_000_000.0


@pytest.mark.asyncio
async def test_scanner_persists_raw_aqrr_subreasons_in_scan_result(monkeypatch) -> None:
    import app.services.scanner as scanner_module

    async def fake_get_settings_map(_session):
        return {
            "risk_per_trade_pct": "2.0",
            "max_portfolio_risk_pct": "6.0",
            "max_leverage": "10",
            "deployable_equity_pct": "90",
            "max_book_spread_bps": "12",
            "min_24h_quote_volume_usdt": "50000000",
            "kill_switch_consecutive_stop_losses": "2",
            "kill_switch_daily_drawdown_pct": "4.0",
            "auto_mode_max_entry_drift_pct": "5.0",
        }

    def fake_evaluate_symbol(**_kwargs):
        return AqrrEvaluation(
            outcome=ScanSymbolOutcome.NO_SETUP,
            direction=SignalDirection.LONG,
            candidates=[],
            reason_text="pullback_no_rejection_evidence",
            filter_reasons=["pullback_no_rejection_evidence"],
            diagnostic={
                "market_state": "BULL_TREND",
                "execution_tier": "TIER_A",
                "aqrr_raw_rejection_reason": "pullback_no_rejection_evidence",
                "aqrr_raw_rejection_reasons": ["pullback_no_rejection_evidence"],
                "aqrr_rejection_stage": "candidate_build",
                "aqrr_setup_diagnostics": {
                    "pullback_continuation": {
                        "candidate_built": False,
                        "raw_rejection_reasons": ["pullback_no_rejection_evidence"],
                    }
                },
            },
        )

    monkeypatch.setattr(scanner_module, "get_settings_map", fake_get_settings_map)
    monkeypatch.setattr(scanner_module, "evaluate_symbol", fake_evaluate_symbol)

    session = FakeSession()
    service = ScannerService(DummyGateway(), WebSocketManager(), DummyOrderManager(), DummyNotifier())

    await service.run_scan(session, trigger_type=TriggerType.AUTO_MODE)
    results = [item for item in session.added if isinstance(item, ScanSymbolResult)]

    assert results
    assert all(result.filter_reasons == ["pullback_no_rejection_evidence"] for result in results)
    assert all(result.extra_context["aqrr_raw_rejection_reason"] == "pullback_no_rejection_evidence" for result in results)
    assert all("pullback_continuation" in result.extra_context["aqrr_setup_diagnostics"] for result in results)


@pytest.mark.asyncio
async def test_scan_cycle_detail_returns_sorted_results_and_workflow() -> None:
    cycle = ScanCycle(
        id=1,
        started_at=datetime(2026, 3, 28, 20, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 3, 28, 20, 20, tzinfo=timezone.utc),
        status=ScanStatus.COMPLETE,
        symbols_scanned=2,
        candidates_found=2,
        signals_qualified=1,
        trigger_type=TriggerType.AUTO_MODE,
        error_message=None,
        progress_pct=100,
    )
    results = [
        ScanSymbolResult(
            id=1,
            scan_cycle_id=cycle.id,
            symbol="ADAUSDT",
            direction=SignalDirection.LONG,
            outcome=ScanSymbolOutcome.CANDIDATE,
            confirmation_score=74,
            final_score=80,
            score_breakdown={},
            extra_context={},
            reason_text="candidate",
            filter_reasons=["correlation_conflict"],
            error_message=None,
        ),
        ScanSymbolResult(
            id=2,
            scan_cycle_id=cycle.id,
            symbol="BTCUSDT",
            direction=SignalDirection.LONG,
            outcome=ScanSymbolOutcome.QUALIFIED,
            confirmation_score=80,
            final_score=92,
            score_breakdown={},
            extra_context={},
            reason_text="qualified",
            filter_reasons=[],
            error_message=None,
        ),
    ]
    workflow = [
        AuditLog(
            id=2,
            timestamp=datetime(2026, 3, 28, 20, 5, tzinfo=timezone.utc),
            event_type="SIGNAL_QUALIFIED",
            level=AuditLevel.INFO,
            symbol="BTCUSDT",
            message="BTCUSDT qualified",
            scan_cycle_id=cycle.id,
            signal_id=1,
            order_id=None,
            details={},
        ),
        AuditLog(
            id=1,
            timestamp=datetime(2026, 3, 28, 20, 0, tzinfo=timezone.utc),
            event_type="SCAN_STARTED",
            level=AuditLevel.INFO,
            symbol=None,
            message="Scan started",
            scan_cycle_id=cycle.id,
            signal_id=None,
            order_id=None,
            details={},
        ),
    ]

    detail = await scan_cycle_detail(1, session=DetailSession(cycle, results=results, workflow=workflow))

    assert detail.detail_available is True
    assert [item.symbol for item in detail.results] == ["BTCUSDT", "ADAUSDT"]
    assert [item.event_type for item in detail.workflow] == ["SCAN_STARTED", "SIGNAL_QUALIFIED"]
