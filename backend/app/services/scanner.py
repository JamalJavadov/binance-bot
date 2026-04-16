import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.core.config import ROOT_DIR
from app.core.logging import get_logger
from app.models.enums import ScanStatus, ScanSymbolOutcome, SignalStatus, TriggerType
from app.models.scan_cycle import ScanCycle
from app.models.scan_symbol_result import ScanSymbolResult
from app.models.signal import Signal
from app.services.audit import record_audit
from app.services.order_manager import OrderManager
from app.services.settings import get_settings_map
from app.services.strategy import PRIMARY_STRATEGY_KEY, PRIMARY_STRATEGY_LABEL
from app.services.strategy.aqrr import evaluate_symbol, rank_candidates, select_candidates
from app.services.strategy.config import StrategyConfig, resolve_strategy_config
from app.services.strategy.indicators import (
    closes,
    percentage_returns,
    required_15m_candles_for_volatility_shock,
)
from app.services.strategy.statistics import (
    build_candidate_stats_bucket,
    calibrated_rank_value,
    load_trade_stats,
)
from app.services.strategy.types import SetupCandidate, closed_candles, parse_klines
from app.services.ws_manager import WebSocketManager

logger = get_logger(__name__)


class ScannerService:
    DIAGNOSTIC_LOG_PATH = ROOT_DIR / "logs" / "diagnostic_scan.log"

    def __init__(
        self,
        gateway,
        ws_manager: WebSocketManager,
        order_manager: OrderManager,
        notifier,
        *,
        market_health=None,
    ) -> None:
        self.gateway = gateway
        self.ws_manager = ws_manager
        self.order_manager = order_manager
        self.notifier = notifier
        self.market_health = market_health
        self._is_running = False

    @staticmethod
    def _candidate_key(symbol: str, direction, entry_price: float) -> tuple[str, str, float]:
        return (symbol.upper(), direction.value, round(float(entry_price), 8))

    @staticmethod
    def _entry_distance_pct(*, mark_price: Decimal, entry_price: Decimal) -> Decimal | None:
        if entry_price <= 0:
            return None
        return abs(mark_price - entry_price) / entry_price

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        return format(value.normalize(), "f") if value != 0 else "0"

    @staticmethod
    def _serialize_diagnostic_value(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): ScannerService._serialize_diagnostic_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [ScannerService._serialize_diagnostic_value(item) for item in value]
        return value

    @classmethod
    def _write_diagnostic_log(cls, diagnostic: dict[str, object]) -> None:
        payload = cls._serialize_diagnostic_value(diagnostic)
        cls.DIAGNOSTIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with cls.DIAGNOSTIC_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

    @classmethod
    def _diagnostic_for_result(cls, result: ScanSymbolResult) -> dict[str, object]:
        diagnostic = dict(result.extra_context or {})
        diagnostic.setdefault("scan_cycle_id", result.scan_cycle_id)
        diagnostic.setdefault("symbol", result.symbol)
        diagnostic.setdefault("direction", result.direction.value if result.direction is not None else None)
        diagnostic.setdefault("confirmation_score", result.confirmation_score)
        diagnostic.setdefault("final_score", result.final_score)
        diagnostic.setdefault("score_breakdown", result.score_breakdown or {})
        diagnostic.setdefault("reason_text", result.reason_text)
        diagnostic.setdefault("rejection_reasons", list(result.filter_reasons or []))
        diagnostic["outcome"] = result.outcome.value
        if result.error_message:
            diagnostic.setdefault("error", result.error_message)
        return diagnostic

    @staticmethod
    def _base_symbol_diagnostic(*, cycle_id: int, symbol: str) -> dict[str, object]:
        return {
            "scan_cycle_id": cycle_id,
            "symbol": symbol,
            "strategy_key": PRIMARY_STRATEGY_KEY,
            "strategy_label": PRIMARY_STRATEGY_LABEL,
            "strategy_version": "aqrr_v1",
            "direction": None,
            "market_state": None,
            "setup_family": None,
            "setup_variant": None,
            "entry_style": None,
            "execution_tier": None,
            "net_r_multiple": None,
            "estimated_cost": None,
            "rank_value": None,
            "quote_volume": None,
            "spread_bps": None,
            "spread_median_bps": None,
            "spread_relative_ratio": None,
            "relative_spread_filter_active": False,
            "relative_spread_sample_count": 0,
            "order_book_unstable_reasons": [],
            "atr_percentile": None,
            "liquidity_floor": None,
            "liquidity_floor_method": None,
            "liquidity_floor_percentile_30": None,
            "configured_min_24h_quote_volume_usdt": None,
            "effective_liquidity_floor": None,
            "aqrr_raw_rejection_reason": None,
            "aqrr_raw_rejection_reasons": [],
            "aqrr_rejection_stage": None,
            "aqrr_setup_diagnostics": {},
            "rejection_reasons": [],
            "selection_rejection_reason": None,
            "reason_text": None,
            "outcome": None,
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "expiry_minutes": None,
            "drift_requalification": False,
        }

    @staticmethod
    def _store_scan_result(
        session,
        *,
        cycle_id: int,
        symbol: str,
        outcome: ScanSymbolOutcome,
        direction=None,
        confirmation_score: int | None = None,
        final_score: int | None = None,
        score_breakdown: dict | None = None,
        reason_text: str | None = None,
        filter_reasons: list[str] | None = None,
        error_message: str | None = None,
        extra_context: dict | None = None,
    ) -> ScanSymbolResult:
        result = ScanSymbolResult(
            scan_cycle_id=cycle_id,
            symbol=symbol,
            direction=direction,
            outcome=outcome,
            confirmation_score=confirmation_score,
            final_score=final_score,
            score_breakdown=score_breakdown or {},
            extra_context=extra_context or {},
            reason_text=reason_text,
            filter_reasons=filter_reasons or [],
            error_message=error_message,
        )
        session.add(result)
        return result

    @staticmethod
    def _active_universe_symbols(exchange_info: dict) -> list[str]:
        symbols: list[str] = []
        for item in exchange_info.get("symbols", []):
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                continue
            if str(item.get("status") or "").upper() != "TRADING":
                continue
            if str(item.get("contractType") or "").upper() != "PERPETUAL":
                continue
            quote_asset = str(item.get("quoteAsset") or "").upper()
            margin_asset = str(item.get("marginAsset") or "").upper()
            if quote_asset not in {"USDT", "USDC"} and margin_asset not in {"USDT", "USDC"}:
                continue
            symbols.append(symbol.upper())
        return symbols

    @staticmethod
    def _ordered_scan_symbols(symbols: list[str], priority_symbols: list[str] | None) -> list[str]:
        priority_list = [
            str(symbol).upper()
            for symbol in (priority_symbols or [])
            if isinstance(symbol, str) and symbol.strip()
        ]
        ordered: list[str] = []
        seen: set[str] = set()
        for symbol in priority_list + symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            ordered.append(symbol)
        return ordered

    @classmethod
    def _scan_universe(
        cls,
        *,
        eligible_symbols: list[str],
        ticker_map: dict[str, dict],
        book_tickers: dict[str, dict],
        liquidity_floor: float,
        config: StrategyConfig,
        priority_symbols: list[str] | None,
    ) -> list[str]:
        ranked_symbols = [
            (symbol, cls._quote_volume(ticker_map.get(symbol)))
            for symbol in eligible_symbols
        ]
        ranked_symbols.sort(key=lambda item: item[1], reverse=True)
        return cls._ordered_scan_symbols([symbol for symbol, _ in ranked_symbols], priority_symbols)

    @staticmethod
    def _ticker_map(rows: list[dict]) -> dict[str, dict]:
        return {
            str(item.get("symbol")).upper(): item
            for item in rows
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        }

    @staticmethod
    def _spread_bps(book_ticker: dict | None) -> float | None:
        if not isinstance(book_ticker, dict):
            return None
        try:
            bid = Decimal(str(book_ticker.get("bidPrice", "0")))
            ask = Decimal(str(book_ticker.get("askPrice", "0")))
        except Exception:
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / Decimal("2")
        if mid <= 0:
            return None
        return float(((ask - bid) / mid) * Decimal("10000"))

    @staticmethod
    def _book_is_stable(book_ticker: dict | None) -> bool:
        if not isinstance(book_ticker, dict):
            return False
        try:
            bid_qty = Decimal(str(book_ticker.get("bidQty", "0")))
            ask_qty = Decimal(str(book_ticker.get("askQty", "0")))
        except Exception:
            return False
        return bid_qty > 0 and ask_qty > 0

    @staticmethod
    def _quote_volume(ticker: dict | None) -> float:
        if not isinstance(ticker, dict):
            return 0.0
        try:
            return float(Decimal(str(ticker.get("quoteVolume", "0"))))
        except Exception:
            return 0.0

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(value for value in values if value > 0)
        if not ordered:
            return 0.0
        rank = max(0, min(len(ordered) - 1, round((percentile / 100.0) * (len(ordered) - 1))))
        return ordered[rank]

    @staticmethod
    def _liquidity_floor(
        *,
        config: StrategyConfig,
        eligible_symbols: list[str],
        ticker_map: dict[str, dict],
    ) -> float:
        return ScannerService._liquidity_floor_diagnostics(
            config=config,
            eligible_symbols=eligible_symbols,
            ticker_map=ticker_map,
        )["effective_liquidity_floor"]

    @staticmethod
    def _liquidity_floor_diagnostics(
        *,
        config: StrategyConfig,
        eligible_symbols: list[str],
        ticker_map: dict[str, dict],
    ) -> dict[str, float | str | None]:
        quote_volumes = [
            ScannerService._quote_volume(ticker_map.get(symbol))
            for symbol in eligible_symbols
            if ScannerService._quote_volume(ticker_map.get(symbol)) > 0
        ]
        percentile_floor = ScannerService._percentile(quote_volumes, 30.0)
        configured_floor = float(config.min_24h_quote_volume_usdt)
        if percentile_floor > 0:
            return {
                "liquidity_floor_method": "percentile_30",
                "liquidity_floor_percentile_30": percentile_floor,
                "configured_min_24h_quote_volume_usdt": configured_floor,
                "effective_liquidity_floor": percentile_floor,
            }
        return {
            "liquidity_floor_method": "fallback_25m",
            "liquidity_floor_percentile_30": None,
            "configured_min_24h_quote_volume_usdt": configured_floor,
            "effective_liquidity_floor": 25_000_000.0,
        }

    async def _btc_returns_1h(self) -> list[float]:
        try:
            raw = await self.gateway.klines("BTCUSDT", "1h", 80)
        except Exception:
            return []
        candles = closed_candles(parse_klines(raw, symbol="BTCUSDT"))
        return percentage_returns(closes(candles))

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

    async def _fresh_mark_payload(self, *, symbol: str, snapshot_mark: dict | None) -> tuple[dict | None, bool]:
        try:
            return await self.gateway.mark_price(symbol), True
        except Exception as exc:
            logger.warning("scan.mark_price_refresh_failed", symbol=symbol, error=str(exc))
            return snapshot_mark, False

    @staticmethod
    def _parse_fee_rate(value: object) -> float | None:
        if value in {None, ""}:
            return None
        try:
            rate = float(Decimal(str(value)))
        except Exception:
            return None
        if rate < 0:
            return None
        return rate

    async def _resolve_account_commission_rates(
        self,
        *,
        symbol: str,
        settings_map: dict[str, str],
        credentials,
    ) -> tuple[float | None, float | None, str, str | None]:
        maker_override = self._parse_fee_rate(settings_map.get("account_maker_fee_rate"))
        taker_override = self._parse_fee_rate(settings_map.get("account_taker_fee_rate"))
        if maker_override is not None or taker_override is not None:
            return maker_override, taker_override, "settings_override", None

        if credentials is None or not hasattr(self.gateway, "commission_rate"):
            return None, None, "strategy_defaults", None

        try:
            payload = await self.gateway.commission_rate(credentials, symbol)
        except Exception as exc:
            return None, None, "strategy_defaults", str(exc)

        if not isinstance(payload, dict):
            return None, None, "strategy_defaults", "commission_rate_payload_invalid"

        maker_rate = self._parse_fee_rate(
            payload.get("makerCommissionRate")
            or payload.get("makerFeeRate")
            or payload.get("maker")
        )
        taker_rate = self._parse_fee_rate(
            payload.get("takerCommissionRate")
            or payload.get("takerFeeRate")
            or payload.get("taker")
        )
        if maker_rate is None and taker_rate is None:
            return None, None, "strategy_defaults", "commission_rate_values_missing"
        return maker_rate, taker_rate, "exchange_account", None

    async def _load_funding_rate_history(
        self,
        *,
        symbol: str,
        funding_rate: float,
        next_funding_time_ms: int | None,
    ) -> tuple[list[float], str | None]:
        if not hasattr(self.gateway, "funding_rate_history"):
            return [], None
        if next_funding_time_ms is not None and abs(funding_rate) >= 0.0004:
            return [], None
        try:
            payload = await self.gateway.funding_rate_history(symbol, limit=12)
        except Exception as exc:
            return [], str(exc)

        if not isinstance(payload, list):
            return [], "funding_rate_history_payload_invalid"
        history_rates: list[float] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                history_rates.append(float(Decimal(str(row.get("fundingRate", "0")))))
            except Exception:
                continue
        return history_rates, None

    def _auto_mode_entry_distance_message(
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

    def _candidate_diagnostic(
        self,
        *,
        base: dict[str, object],
        candidate: SetupCandidate,
        evaluation_diagnostic: dict[str, object],
        filter_reasons: list[str],
        reason_text: str,
        preview: dict | None,
        quote_volume: float,
        spread_bps: float,
        spread_median_bps: float | None,
        spread_relative_ratio: float | None,
        relative_spread_filter_active: bool,
        relative_spread_sample_count: int,
        liquidity_floor: float,
        drift_requalification: bool,
        mark_payload: dict | None,
        mark_price_fresh: bool,
    ) -> dict[str, object]:
        extra_context = {
            **base,
            **evaluation_diagnostic,
            **candidate.extra_context,
            "direction": candidate.direction.value,
            "market_state": candidate.market_state,
            "setup_family": candidate.setup_family,
            "setup_variant": candidate.setup_variant,
            "entry_style": candidate.entry_style,
            "execution_tier": candidate.execution_tier,
            "net_r_multiple": round(candidate.net_r_multiple, 4),
            "estimated_cost": round(candidate.estimated_cost, 8),
            "rank_value": round(candidate.rank_value, 4),
            "score_band": candidate.extra_context.get("score_band"),
            "volatility_band": candidate.extra_context.get("volatility_band"),
            "stats_bucket_key": candidate.extra_context.get("stats_bucket_key"),
            "strategy_context": candidate.extra_context.get("strategy_context", {}),
            "quote_volume": round(quote_volume, 2),
            "spread_bps": round(spread_bps, 4),
            "spread_median_bps": None if spread_median_bps is None else round(spread_median_bps, 4),
            "spread_relative_ratio": None if spread_relative_ratio is None else round(spread_relative_ratio, 4),
            "relative_spread_filter_active": relative_spread_filter_active,
            "relative_spread_sample_count": relative_spread_sample_count,
            "liquidity_floor": round(liquidity_floor, 2),
            "effective_liquidity_floor": round(liquidity_floor, 2),
            "entry_price": candidate.entry_price,
            "stop_loss": candidate.stop_loss,
            "take_profit": candidate.take_profit,
            "expiry_minutes": candidate.expiry_minutes,
            "score_breakdown": candidate.score_breakdown,
            "rejection_reasons": list(filter_reasons),
            "reason_text": reason_text,
            "drift_requalification": drift_requalification,
            "mark_price_snapshot": mark_payload,
            "mark_price_fresh": mark_price_fresh,
        }
        if preview is not None:
            extra_context["order_preview"] = preview
        return extra_context

    @staticmethod
    def _candidate_with_stats(
        candidate: SetupCandidate,
        *,
        atr_percentile: float | None,
        stats_map: dict[str, object],
    ) -> SetupCandidate:
        bucket = build_candidate_stats_bucket(
            setup_family=candidate.setup_family,
            direction=candidate.direction,
            market_state=candidate.market_state,
            execution_tier=candidate.execution_tier,
            final_score=candidate.final_score,
            atr_percentile=atr_percentile,
        )
        stat = stats_map.get(bucket.bucket_key)
        calibrated_rank, hit_rate_score = calibrated_rank_value(final_score=candidate.final_score, stat=stat)
        extra_context = {
            **candidate.extra_context,
            "score_band": bucket.score_band,
            "volatility_band": bucket.volatility_band,
            "stats_bucket_key": bucket.bucket_key,
            "rank_method": "calibrated_hit_rate" if hit_rate_score is not None else "final_score",
            "calibrated_hit_rate_score": None if hit_rate_score is None else round(hit_rate_score, 4),
        }
        selection_context = {
            **candidate.selection_context,
            "score_band": bucket.score_band,
            "volatility_band": bucket.volatility_band,
            "stats_bucket_key": bucket.bucket_key,
        }
        return SetupCandidate(
            **{
                **candidate.__dict__,
                "rank_value": calibrated_rank,
                "extra_context": extra_context,
                "selection_context": selection_context,
            }
        )

    async def run_scan(self, session, *, trigger_type, priority_symbols: list[str] | None = None) -> ScanCycle:
        if trigger_type != TriggerType.AUTO_MODE:
            raise ValueError("Only AUTO_MODE scans are supported")
        if self._is_running:
            raise RuntimeError("Scan already in progress")

        self._is_running = True
        cycle = ScanCycle(
            started_at=datetime.now(timezone.utc),
            trigger_type=trigger_type,
        )
        session.add(cycle)
        await session.commit()
        await session.refresh(cycle)

        try:
            await record_audit(
                session,
                event_type="SCAN_STARTED",
                message=f"Scan started via {trigger_type.value.lower()}",
                scan_cycle_id=cycle.id,
                details={"trigger_type": trigger_type.value, "strategy_key": PRIMARY_STRATEGY_KEY},
            )
            await session.commit()

            settings_map = await get_settings_map(session)
            config = resolve_strategy_config(settings_map)
            exchange_info = await self.gateway.exchange_info()
            filters_map = self.gateway.parse_symbol_filters(exchange_info)
            credentials = await self.order_manager.get_credentials(session)
            account_snapshot = await self.order_manager.get_account_snapshot(session, credentials)
            active_entry_orders = await self.order_manager.active_entry_orders(session)
            shared_slot_budget = self.order_manager.build_shared_entry_slot_budget(
                available_balance=account_snapshot.available_balance,
                active_entry_orders=active_entry_orders,
            )
            active_risk_usdt = sum(
                (Decimal(order.risk_usdt_at_stop or 0) for order in active_entry_orders),
                start=Decimal("0"),
            )
            portfolio_risk_cap_usdt = account_snapshot.available_balance * config.max_portfolio_risk_fraction
            remaining_portfolio_risk_usdt = max(portfolio_risk_cap_usdt - active_risk_usdt, Decimal("0"))
            evaluation_remaining_slots = max(shared_slot_budget.remaining_entry_slots, 1)
            per_trade_risk_cap_usdt = account_snapshot.available_balance * config.risk_per_trade_fraction
            target_risk_usdt = min(
                per_trade_risk_cap_usdt,
                remaining_portfolio_risk_usdt / Decimal(evaluation_remaining_slots)
                if remaining_portfolio_risk_usdt > 0
                else Decimal("0"),
            )
            leverage_brackets_map = await self.gateway.leverage_brackets(credentials) if credentials is not None else {}
            ticker_map = self._ticker_map(await self.gateway.ticker_24hr())
            book_tickers = await self.gateway.book_tickers()
            mark_prices = await self.gateway.mark_prices()
            eligible_symbols = self._active_universe_symbols(exchange_info)
            liquidity_floor_diagnostic = self._liquidity_floor_diagnostics(
                config=config,
                eligible_symbols=eligible_symbols,
                ticker_map=ticker_map,
            )
            liquidity_floor = float(liquidity_floor_diagnostic["effective_liquidity_floor"])
            symbols = self._scan_universe(
                eligible_symbols=eligible_symbols,
                ticker_map=ticker_map,
                book_tickers=book_tickers,
                liquidity_floor=liquidity_floor,
                config=config,
                priority_symbols=priority_symbols,
            )
            btc_returns_1h = await self._btc_returns_1h()
            candidates: list[SetupCandidate] = []
            candidate_results: dict[tuple[str, str, float], ScanSymbolResult] = {}
            scan_results_to_log: list[ScanSymbolResult] = []
            priority_symbol_set = {
                str(symbol).upper()
                for symbol in (priority_symbols or [])
                if isinstance(symbol, str) and symbol.strip()
            }

            for index, symbol in enumerate(symbols, start=1):
                result_saved = False
                symbol_diagnostic = self._base_symbol_diagnostic(cycle_id=cycle.id, symbol=symbol)
                symbol_diagnostic.update(liquidity_floor_diagnostic)
                symbol_diagnostic["liquidity_floor"] = round(liquidity_floor, 2)
                try:
                    filters = filters_map.get(symbol)
                    if filters is None:
                        symbol_diagnostic.update(
                            {
                                "reason_text": "symbol_missing_exchange_filters",
                                "rejection_reasons": ["symbol_missing_exchange_filters"],
                                "outcome": ScanSymbolOutcome.UNSUPPORTED.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.UNSUPPORTED,
                            reason_text="symbol_missing_exchange_filters",
                            filter_reasons=["symbol_missing_exchange_filters"],
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue

                    ticker = ticker_map.get(symbol)
                    book_ticker = book_tickers.get(symbol)
                    mark_snapshot = mark_prices.get(symbol)
                    market_health_snapshot = (
                        await self.market_health.snapshot(
                            symbol,
                            fallback_book_ticker=book_ticker,
                            fallback_mark_price=mark_snapshot,
                        )
                        if self.market_health is not None
                        else None
                    )
                    book_ticker = market_health_snapshot.book_ticker if market_health_snapshot is not None else book_ticker
                    mark_snapshot = market_health_snapshot.mark_price if market_health_snapshot is not None else mark_snapshot
                    quote_volume = self._quote_volume(ticker)
                    spread_bps = market_health_snapshot.spread_bps if market_health_snapshot is not None else self._spread_bps(book_ticker)
                    spread_median_bps = market_health_snapshot.spread_median_bps if market_health_snapshot is not None else None
                    spread_relative_ratio = market_health_snapshot.spread_relative_ratio if market_health_snapshot is not None else None
                    relative_spread_filter_active = market_health_snapshot.relative_spread_ready if market_health_snapshot is not None else False
                    relative_spread_sample_count = market_health_snapshot.relative_spread_sample_count if market_health_snapshot is not None else 0
                    book_is_stable = market_health_snapshot.book_stable if market_health_snapshot is not None else self._book_is_stable(book_ticker)
                    book_stability_reasons = (
                        list(getattr(market_health_snapshot, "book_stability_reasons", ()) or [])
                        if market_health_snapshot is not None
                        else []
                    )
                    symbol_diagnostic.update(
                        {
                            "quote_volume": round(quote_volume, 2),
                            "spread_bps": None if spread_bps is None else round(spread_bps, 4),
                            "spread_median_bps": None if spread_median_bps is None else round(spread_median_bps, 4),
                            "spread_relative_ratio": None if spread_relative_ratio is None else round(spread_relative_ratio, 4),
                            "relative_spread_filter_active": relative_spread_filter_active,
                            "relative_spread_sample_count": relative_spread_sample_count,
                            "order_book_unstable_reasons": book_stability_reasons,
                            "liquidity_floor": round(liquidity_floor, 2),
                            "effective_liquidity_floor": round(liquidity_floor, 2),
                        }
                    )
                    if not book_is_stable:
                        symbol_diagnostic.update(
                            {
                                "reason_text": "order_book_unstable",
                                "rejection_reasons": ["order_book_unstable"],
                                "outcome": ScanSymbolOutcome.FILTERED_OUT.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.FILTERED_OUT,
                            reason_text="order_book_unstable",
                            filter_reasons=["order_book_unstable"],
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue
                    if spread_bps is None:
                        symbol_diagnostic.update(
                            {
                                "reason_text": "spread_unavailable",
                                "rejection_reasons": ["spread_unavailable"],
                                "outcome": ScanSymbolOutcome.FILTERED_OUT.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.FILTERED_OUT,
                            reason_text="spread_unavailable",
                            filter_reasons=["spread_unavailable"],
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue
                    if spread_bps > float(config.max_book_spread_bps):
                        symbol_diagnostic.update(
                            {
                                "reason_text": "spread_above_threshold",
                                "rejection_reasons": ["spread_above_threshold"],
                                "outcome": ScanSymbolOutcome.FILTERED_OUT.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.FILTERED_OUT,
                            reason_text="spread_above_threshold",
                            filter_reasons=["spread_above_threshold"],
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue
                    if (
                        relative_spread_filter_active
                        and spread_relative_ratio is not None
                        and spread_relative_ratio > 2.5
                    ):
                        symbol_diagnostic.update(
                            {
                                "reason_text": "spread_relative_above_threshold",
                                "rejection_reasons": ["spread_relative_above_threshold"],
                                "outcome": ScanSymbolOutcome.FILTERED_OUT.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.FILTERED_OUT,
                            reason_text="spread_relative_above_threshold",
                            filter_reasons=["spread_relative_above_threshold"],
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue
                    if quote_volume < liquidity_floor:
                        symbol_diagnostic.update(
                            {
                                "reason_text": "quote_volume_below_liquidity_floor",
                                "rejection_reasons": ["quote_volume_below_liquidity_floor"],
                                "outcome": ScanSymbolOutcome.FILTERED_OUT.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.FILTERED_OUT,
                            reason_text="quote_volume_below_liquidity_floor",
                            filter_reasons=["quote_volume_below_liquidity_floor"],
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue

                    candles_15m = await self._load_closed_candles(
                        symbol=symbol,
                        interval="15m",
                        limit=max(260, required_15m_candles_for_volatility_shock(atr_period=config.atr_period_15m)),
                    )
                    candles_1h = await self._load_closed_candles(symbol=symbol, interval="1h", limit=260)
                    candles_4h = await self._load_closed_candles(symbol=symbol, interval="4h", limit=260)
                    if (
                        len(candles_15m) < 60
                        or len(candles_1h) < config.ema_context_period + 5
                        or len(candles_4h) < config.ema_context_period + 5
                    ):
                        symbol_diagnostic.update(
                            {
                                "reason_text": "insufficient_closed_candles",
                                "rejection_reasons": ["insufficient_closed_candles"],
                                "outcome": ScanSymbolOutcome.NO_SETUP.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.NO_SETUP,
                            reason_text="insufficient_closed_candles",
                            filter_reasons=["insufficient_closed_candles"],
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue
                    current_price = float((mark_snapshot or {}).get("markPrice") or candles_15m[-1].close)
                    funding_rate = float((mark_snapshot or {}).get("lastFundingRate") or 0.0)
                    try:
                        raw_next_funding_time = (mark_snapshot or {}).get("nextFundingTime")
                        next_funding_time_ms = int(raw_next_funding_time) if raw_next_funding_time not in {None, ""} else None
                    except (TypeError, ValueError):
                        next_funding_time_ms = None
                    (
                        account_maker_fee_rate,
                        account_taker_fee_rate,
                        commission_source,
                        commission_lookup_error,
                    ) = await self._resolve_account_commission_rates(
                        symbol=symbol,
                        settings_map=settings_map,
                        credentials=credentials,
                    )
                    funding_rate_history, funding_history_lookup_error = await self._load_funding_rate_history(
                        symbol=symbol,
                        funding_rate=funding_rate,
                        next_funding_time_ms=next_funding_time_ms,
                    )
                    evaluation = evaluate_symbol(
                        symbol=symbol,
                        candles_15m=candles_15m,
                        candles_1h=candles_1h,
                        candles_4h=candles_4h,
                        current_price=current_price,
                        funding_rate=funding_rate,
                        quote_volume=quote_volume,
                        spread_bps=spread_bps,
                        spread_relative_ratio=spread_relative_ratio,
                        relative_spread_ready=relative_spread_filter_active,
                        liquidity_floor=liquidity_floor,
                        filters_min_notional=float(filters.min_notional),
                        tick_size=float(filters.tick_size),
                        available_balance=float(account_snapshot.available_balance),
                        config=config,
                        btc_returns_1h=btc_returns_1h,
                        next_funding_time_ms=next_funding_time_ms,
                        account_maker_fee_rate=account_maker_fee_rate,
                        account_taker_fee_rate=account_taker_fee_rate,
                        funding_rate_history=funding_rate_history,
                        remaining_entry_slots=evaluation_remaining_slots,
                        remaining_portfolio_risk_usdt=float(remaining_portfolio_risk_usdt),
                    )
                    symbol_diagnostic.update(
                        {
                            "direction": evaluation.direction.value if evaluation.direction is not None else None,
                            "market_state": evaluation.diagnostic.get("market_state"),
                            "execution_tier": evaluation.diagnostic.get("execution_tier"),
                            "commission_source": commission_source,
                            "account_maker_fee_rate": account_maker_fee_rate,
                            "account_taker_fee_rate": account_taker_fee_rate,
                            "funding_rate_history_points": len(funding_rate_history),
                        }
                    )
                    if commission_lookup_error is not None:
                        symbol_diagnostic["commission_lookup_error"] = commission_lookup_error
                    if funding_history_lookup_error is not None:
                        symbol_diagnostic["funding_history_lookup_error"] = funding_history_lookup_error
                    if not evaluation.candidates:
                        symbol_diagnostic.update(
                            {
                                **evaluation.diagnostic,
                                "reason_text": evaluation.reason_text,
                                "rejection_reasons": list(evaluation.filter_reasons),
                                "outcome": evaluation.outcome.value,
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            direction=evaluation.direction,
                            outcome=evaluation.outcome,
                            reason_text=evaluation.reason_text,
                            filter_reasons=evaluation.filter_reasons,
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue

                    atr_percentile = evaluation.diagnostic.get("atr_percentile")
                    bucket_keys = [
                        build_candidate_stats_bucket(
                            setup_family=item.setup_family,
                            direction=item.direction,
                            market_state=item.market_state,
                            execution_tier=item.execution_tier,
                            final_score=item.final_score,
                            atr_percentile=atr_percentile if isinstance(atr_percentile, (float, int)) else None,
                        ).bucket_key
                        for item in evaluation.candidates
                    ]
                    stats_map = await load_trade_stats(session, bucket_keys=bucket_keys)
                    symbol_candidates = rank_candidates(
                        [
                            self._candidate_with_stats(
                                candidate,
                                atr_percentile=atr_percentile if isinstance(atr_percentile, (float, int)) else None,
                                stats_map=stats_map,
                            )
                            for candidate in evaluation.candidates
                        ]
                    )

                    best_candidate: SetupCandidate | None = None
                    best_failure: dict[str, object] | None = None
                    for candidate in symbol_candidates:
                        fresh_mark_payload, mark_price_fresh = await self._fresh_mark_payload(
                            symbol=symbol,
                            snapshot_mark=mark_snapshot,
                        )
                        live_mark_price = Decimal(
                            str((fresh_mark_payload or {}).get("markPrice") or current_price)
                        )
                        execution = self.order_manager.build_execution_plan(
                            symbol=symbol,
                            account_snapshot=account_snapshot,
                            settings_map=settings_map,
                            filters=filters,
                            entry_style=candidate.entry_style,
                            direction=candidate.direction,
                            entry_price=Decimal(str(candidate.entry_price)),
                            stop_loss=Decimal(str(candidate.stop_loss)),
                            take_profit=Decimal(str(candidate.take_profit)),
                            mark_price=live_mark_price,
                            leverage_brackets=leverage_brackets_map.get(symbol, []),
                            risk_budget_override_usdt=shared_slot_budget.per_slot_budget,
                            target_risk_usdt_override=target_risk_usdt,
                            estimated_cost=Decimal(str(candidate.estimated_cost)),
                            use_stop_distance_position_sizing=True,
                        )
                        market_validation = execution.get("market_state")
                        preview = execution.get("order_preview")
                        filter_reasons: list[str] = []
                        reason_text = candidate.reason_text
                        if execution.get("error"):
                            filter_reasons.append(str(execution.get("error")))
                            reason_text = (
                                self.order_manager._preview_error_message(symbol, execution)
                            )
                        elif market_validation is not None and market_validation.stale_reason:
                            filter_reasons.append(str(market_validation.stale_reason))
                            reason_text = str(
                                market_validation.message
                                or f"{symbol} order could not be placed because the setup is already stale."
                            )
                        elif preview is None or not preview.get("can_place", False):
                            filter_reasons.append("preview_not_placeable")
                            reason_text = str((preview or {}).get("reason") or "preview_not_placeable")
                        else:
                            distance_pct = self._entry_distance_pct(
                                mark_price=live_mark_price,
                                entry_price=Decimal(str(candidate.entry_price)),
                            )
                            if (
                                distance_pct is not None
                                and distance_pct > config.auto_mode_max_entry_distance_fraction
                            ):
                                filter_reasons.append("entry_too_far_from_mark")
                                reason_text = self._auto_mode_entry_distance_message(
                                    symbol=symbol,
                                    mark_price=live_mark_price,
                                    entry_price=Decimal(str(candidate.entry_price)),
                                    distance_pct=distance_pct,
                                    max_distance_pct=config.auto_mode_max_entry_distance_fraction,
                                )

                        candidate_diagnostic = self._candidate_diagnostic(
                            base=symbol_diagnostic,
                            candidate=candidate,
                            evaluation_diagnostic=evaluation.diagnostic,
                            filter_reasons=filter_reasons,
                            reason_text=reason_text,
                            preview=preview,
                            quote_volume=quote_volume,
                            spread_bps=spread_bps,
                            spread_median_bps=spread_median_bps,
                            spread_relative_ratio=spread_relative_ratio,
                            relative_spread_filter_active=relative_spread_filter_active,
                            relative_spread_sample_count=relative_spread_sample_count,
                            liquidity_floor=liquidity_floor,
                            drift_requalification=symbol in priority_symbol_set,
                            mark_payload=fresh_mark_payload,
                            mark_price_fresh=mark_price_fresh,
                        )
                        if filter_reasons:
                            if best_failure is None or candidate.rank_value > float(best_failure["rank_value"]):
                                best_failure = {
                                    "direction": candidate.direction,
                                    "confirmation_score": candidate.confirmation_score,
                                    "final_score": candidate.final_score,
                                    "score_breakdown": candidate.score_breakdown,
                                    "reason_text": reason_text,
                                    "filter_reasons": filter_reasons,
                                    "extra_context": {
                                        **candidate_diagnostic,
                                        "outcome": ScanSymbolOutcome.FILTERED_OUT.value,
                                    },
                                    "rank_value": candidate.rank_value,
                                }
                            continue

                        best_candidate = SetupCandidate(
                            **{
                                **candidate.__dict__,
                                "current_price": float(live_mark_price),
                                "order_preview": preview,
                                "extra_context": {
                                    **candidate_diagnostic,
                                    "outcome": ScanSymbolOutcome.CANDIDATE.value,
                                },
                            }
                        )
                        break

                    if best_candidate is None:
                        failure = best_failure or {
                            "direction": evaluation.direction,
                            "confirmation_score": None,
                            "final_score": None,
                            "score_breakdown": {},
                            "reason_text": evaluation.reason_text or "aqrr_hard_filters_failed",
                            "filter_reasons": evaluation.filter_reasons or ["aqrr_hard_filters_failed"],
                            "extra_context": {
                                **symbol_diagnostic,
                                **evaluation.diagnostic,
                                "outcome": ScanSymbolOutcome.FILTERED_OUT.value,
                            },
                        }
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            direction=failure["direction"],
                            outcome=ScanSymbolOutcome.FILTERED_OUT,
                            confirmation_score=failure["confirmation_score"],
                            final_score=failure["final_score"],
                            score_breakdown=failure["score_breakdown"],
                            reason_text=failure["reason_text"],
                            filter_reasons=failure["filter_reasons"],
                            extra_context=failure["extra_context"],
                        )
                        scan_results_to_log.append(result)
                        result_saved = True
                        continue

                    candidates.append(best_candidate)
                    candidate_result = self._store_scan_result(
                        session,
                        cycle_id=cycle.id,
                        symbol=symbol,
                        direction=best_candidate.direction,
                        outcome=ScanSymbolOutcome.CANDIDATE,
                        confirmation_score=best_candidate.confirmation_score,
                        final_score=best_candidate.final_score,
                        score_breakdown=best_candidate.score_breakdown,
                        reason_text=best_candidate.reason_text,
                        extra_context=best_candidate.extra_context,
                    )
                    scan_results_to_log.append(candidate_result)
                    candidate_results[
                        self._candidate_key(symbol, best_candidate.direction, best_candidate.entry_price)
                    ] = candidate_result
                    result_saved = True
                except Exception as exc:
                    logger.warning("scan.symbol_failed", symbol=symbol, error=str(exc))
                    await record_audit(
                        session,
                        event_type="SCAN_SYMBOL_FAILED",
                        message=f"{symbol} failed during scan",
                        symbol=symbol,
                        scan_cycle_id=cycle.id,
                        details={"error": str(exc), "strategy_key": PRIMARY_STRATEGY_KEY},
                    )
                    if not result_saved:
                        symbol_diagnostic.update(
                            {
                                "reason_text": str(exc),
                                "rejection_reasons": [],
                                "outcome": ScanSymbolOutcome.FAILED.value,
                                "error": str(exc),
                            }
                        )
                        result = self._store_scan_result(
                            session,
                            cycle_id=cycle.id,
                            symbol=symbol,
                            outcome=ScanSymbolOutcome.FAILED,
                            error_message=str(exc),
                            extra_context=symbol_diagnostic,
                        )
                        scan_results_to_log.append(result)
                finally:
                    cycle.symbols_scanned = index
                    cycle.progress_pct = round(index / max(len(symbols), 1) * 100, 2)
                    cycle.candidates_found = len(candidates)
                    await session.commit()
                    await self.ws_manager.broadcast(
                        "scan_progress",
                        {
                            "cycle_id": cycle.id,
                            "symbols_scanned": cycle.symbols_scanned,
                            "progress_pct": cycle.progress_pct,
                        },
                    )

            selection = select_candidates(candidates, config=config)
            qualified = selection.selected
            qualified_keys = {
                self._candidate_key(candidate.symbol, candidate.direction, candidate.entry_price)
                for candidate in qualified
            }
            ranked_candidates = rank_candidates(candidates)
            for candidate in ranked_candidates:
                candidate_key = self._candidate_key(candidate.symbol, candidate.direction, candidate.entry_price)
                status = SignalStatus.QUALIFIED if candidate_key in qualified_keys else SignalStatus.CANDIDATE
                selection_rejection_reason = selection.rejected.get(candidate_key)
                candidate_result = candidate_results.get(candidate_key)
                if candidate_result is not None:
                    candidate_result.outcome = (
                        ScanSymbolOutcome.QUALIFIED if status == SignalStatus.QUALIFIED else ScanSymbolOutcome.CANDIDATE
                    )
                    candidate_result.extra_context = {
                        **(candidate_result.extra_context or {}),
                        "selection_rejection_reason": selection_rejection_reason,
                        "outcome": candidate_result.outcome.value,
                    }

                    signal = Signal(
                        scan_cycle_id=cycle.id,
                        symbol=candidate.symbol,
                        direction=candidate.direction,
                        timeframe=candidate.timeframe,
                    entry_price=Decimal(str(candidate.entry_price)),
                    stop_loss=Decimal(str(candidate.stop_loss)),
                    take_profit=Decimal(str(candidate.take_profit)),
                        rr_ratio=Decimal(str(round(candidate.net_r_multiple, 2))),
                        confirmation_score=candidate.confirmation_score,
                        final_score=int(candidate.final_score),
                        rank_value=Decimal(str(round(candidate.rank_value, 4))),
                        net_r_multiple=Decimal(str(round(candidate.net_r_multiple, 4))),
                        estimated_cost=Decimal(str(candidate.estimated_cost)),
                        score_breakdown=candidate.score_breakdown,
                        reason_text=candidate.reason_text,
                        swing_origin=Decimal(str(candidate.swing_origin)),
                        swing_terminus=Decimal(str(candidate.swing_terminus)),
                        fib_0786_level=None,
                        current_price_at_signal=Decimal(str(candidate.current_price)),
                        entry_style=candidate.entry_style,
                        setup_family=candidate.setup_family,
                        setup_variant=candidate.setup_variant,
                        market_state=candidate.market_state,
                        execution_tier=candidate.execution_tier,
                        score_band=candidate.extra_context.get("score_band"),
                        volatility_band=candidate.extra_context.get("volatility_band"),
                        stats_bucket_key=candidate.extra_context.get("stats_bucket_key"),
                        strategy_context=dict(candidate.extra_context.get("strategy_context") or {}),
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=candidate.expiry_minutes),
                        status=status,
                        extra_context={
                        **candidate.extra_context,
                        "strategy_key": PRIMARY_STRATEGY_KEY,
                        "strategy_label": PRIMARY_STRATEGY_LABEL,
                        "selection_rejection_reason": selection_rejection_reason,
                        "order_preview": candidate.order_preview,
                    },
                )
                session.add(signal)
                await session.flush()
                if status == SignalStatus.QUALIFIED:
                    await record_audit(
                        session,
                        event_type="SIGNAL_QUALIFIED",
                        message=f"{signal.symbol} qualified",
                        symbol=signal.symbol,
                        scan_cycle_id=cycle.id,
                        signal_id=signal.id,
                        details={
                            "direction": signal.direction.value,
                            "final_score": signal.final_score,
                            "strategy_key": PRIMARY_STRATEGY_KEY,
                        },
                    )
                    await self.ws_manager.broadcast(
                        "signal_found",
                        {"signal_id": signal.id, "symbol": signal.symbol, "direction": signal.direction.value},
                    )

            for result in scan_results_to_log:
                self._write_diagnostic_log(self._diagnostic_for_result(result))

            cycle.status = ScanStatus.COMPLETE
            cycle.completed_at = datetime.now(timezone.utc)
            cycle.signals_qualified = len(qualified)
            await record_audit(
                session,
                event_type="SCAN_COMPLETE",
                message=f"Scan complete with {len(qualified)} qualified signals",
                scan_cycle_id=cycle.id,
                    details={
                        "qualified": len(qualified),
                        "qualified_count": len(qualified),
                        "candidates": len(candidates),
                        "candidate_count": len(candidates),
                        "scan_scope": "all_eligible_symbols",
                        "strategy_key": PRIMARY_STRATEGY_KEY,
                    },
                )
            await session.commit()

            if qualified:
                signal_summary = ", ".join(f"{item.symbol} {item.direction.value}" for item in qualified)
                await self.notifier.send(title="New Signals Ready", message=signal_summary, sound="signal")

            await self.ws_manager.broadcast(
                "scan_complete",
                {
                    "cycle_id": cycle.id,
                    "qualified_signals": len(qualified),
                    "candidates_found": len(candidates),
                },
            )
            return cycle
        except asyncio.CancelledError:
            cycle.status = ScanStatus.FAILED
            cycle.error_message = "Scan cancelled"
            cycle.completed_at = datetime.now(timezone.utc)
            await record_audit(
                session,
                event_type="SCAN_FAILED",
                message="Scan cancelled",
                scan_cycle_id=cycle.id,
                details={"trigger_type": trigger_type.value, "reason": "cancelled"},
            )
            await session.commit()
            raise
        except Exception as exc:
            cycle.status = ScanStatus.FAILED
            cycle.error_message = str(exc)
            cycle.completed_at = datetime.now(timezone.utc)
            await record_audit(
                session,
                event_type="SCAN_FAILED",
                message=str(exc),
                scan_cycle_id=cycle.id,
                details={"strategy_key": PRIMARY_STRATEGY_KEY},
            )
            await session.commit()
            raise
        finally:
            self._is_running = False
