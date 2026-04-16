from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

from app.core.logging import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class MarketHealthSnapshot:
    symbol: str
    book_ticker: dict | None
    mark_price: dict | None
    spread_bps: float | None
    spread_median_bps: float | None
    spread_relative_ratio: float | None
    relative_spread_ready: bool
    relative_spread_sample_count: int
    book_stable: bool
    book_stability_reasons: tuple[str, ...]
    touch_notional_usdt: float | None
    quote_velocity_bps: float | None
    mark_gap_bps: float | None
    last_updated_at: datetime | None


@dataclass
class _SymbolMarketState:
    book_ticker: dict | None = None
    mark_price: dict | None = None
    spread_history: deque[tuple[datetime, float]] = field(default_factory=deque)
    book_history: deque[tuple[datetime, float, float, float]] = field(default_factory=deque)
    spread_median_bps: float | None = None
    last_updated_at: datetime | None = None
    last_sampled_at: datetime | None = None


class MarketHealthService:
    POLL_SECONDS = 3
    HISTORY_WINDOW = timedelta(hours=24)
    HISTORY_SAMPLE_SECONDS = 30
    MIN_RELATIVE_SPREAD_SAMPLES = 60
    BOOK_HISTORY_WINDOW = timedelta(minutes=5)
    BOOK_STABILITY_WINDOW = timedelta(seconds=30)
    MIN_TOUCH_NOTIONAL_USDT = 15.0
    ERRATIC_QUOTE_MOVE_BPS = 8.0
    MARK_GAP_FLOOR_BPS = 12.0

    def __init__(self, gateway) -> None:
        self.gateway = gateway
        self._states: dict[str, _SymbolMarketState] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    @staticmethod
    def _spread_bps(book_ticker: dict | None) -> float | None:
        if not isinstance(book_ticker, dict):
            return None
        try:
            bid = float(book_ticker.get("bidPrice", "0"))
            ask = float(book_ticker.get("askPrice", "0"))
        except Exception:
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return ((ask - bid) / mid) * 10000.0

    @staticmethod
    def _book_metrics(book_ticker: dict | None) -> tuple[float, float, float, float, float, float] | None:
        if not isinstance(book_ticker, dict):
            return None
        try:
            bid = float(book_ticker.get("bidPrice", "0"))
            ask = float(book_ticker.get("askPrice", "0"))
            bid_qty = float(book_ticker.get("bidQty", "0"))
            ask_qty = float(book_ticker.get("askQty", "0"))
        except Exception:
            return None
        if bid <= 0 or ask <= 0 or ask < bid or bid_qty <= 0 or ask_qty <= 0:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        spread_bps = ((ask - bid) / mid) * 10000.0
        return bid, ask, bid_qty, ask_qty, mid, spread_bps

    @classmethod
    def _touch_notional_usdt(cls, book_ticker: dict | None) -> float | None:
        metrics = cls._book_metrics(book_ticker)
        if metrics is None:
            return None
        bid, ask, bid_qty, ask_qty, _mid, _spread_bps = metrics
        return min(bid * bid_qty, ask * ask_qty)

    @classmethod
    def _book_stability_assessment(
        cls,
        state: _SymbolMarketState,
        *,
        book_ticker: dict | None,
        mark_price: dict | None,
    ) -> tuple[bool, tuple[str, ...], float | None, float | None, float | None]:
        metrics = cls._book_metrics(book_ticker)
        if metrics is None:
            return False, ("invalid_touch",), None, None, None

        _bid, _ask, _bid_qty, _ask_qty, mid, spread_bps = metrics
        touch_notional_usdt = cls._touch_notional_usdt(book_ticker)
        reasons: list[str] = []
        if touch_notional_usdt is not None and touch_notional_usdt < cls.MIN_TOUCH_NOTIONAL_USDT:
            reasons.append("touch_liquidity_thin")

        recent_cutoff = datetime.now(timezone.utc) - cls.BOOK_STABILITY_WINDOW
        recent_samples = [sample for sample in state.book_history if sample[0] >= recent_cutoff]
        quote_velocity_bps: float | None = None
        if len(recent_samples) >= 3:
            mids = [sample[1] for sample in recent_samples]
            spreads = [sample[2] for sample in recent_samples]
            mid_move_bps: list[float] = []
            move_signs: list[int] = []
            for previous_mid, current_mid in zip(mids, mids[1:]):
                if previous_mid <= 0 or current_mid <= 0:
                    continue
                move_bps = ((current_mid - previous_mid) / previous_mid) * 10000.0
                abs_move_bps = abs(move_bps)
                mid_move_bps.append(abs_move_bps)
                if abs_move_bps >= max(spread_bps, 1.0):
                    move_signs.append(1 if move_bps > 0 else -1)
            if mid_move_bps:
                quote_velocity_bps = max(mid_move_bps)
            direction_changes = sum(
                1
                for previous_sign, current_sign in zip(move_signs, move_signs[1:])
                if previous_sign != current_sign
            )
            spread_median = median(spreads) if spreads else None
            spread_ratio = (
                (max(spreads) / spread_median)
                if spread_median is not None and spread_median > 0
                else None
            )
            if quote_velocity_bps is not None and quote_velocity_bps >= max(cls.ERRATIC_QUOTE_MOVE_BPS, spread_bps * 3.5):
                if direction_changes >= 1:
                    reasons.append("erratic_quote_movement")
            if spread_ratio is not None and spread_ratio >= 2.5 and direction_changes >= 1:
                reasons.append("spread_whipsaw")

        mark_gap_bps: float | None = None
        if isinstance(mark_price, dict):
            try:
                mark = float(mark_price.get("markPrice", "0"))
            except Exception:
                mark = 0.0
            if mark > 0:
                mark_gap_bps = abs(mid - mark) / mark * 10000.0
                if mark_gap_bps >= max(cls.MARK_GAP_FLOOR_BPS, spread_bps * 4.0):
                    reasons.append("book_mark_divergence")

        deduped_reasons = tuple(dict.fromkeys(reasons))
        return len(deduped_reasons) == 0, deduped_reasons, touch_notional_usdt, quote_velocity_bps, mark_gap_bps

    async def _refresh_once(self) -> None:
        book_tickers, mark_prices = await asyncio.gather(
            self.gateway.book_tickers(),
            self.gateway.mark_prices(),
        )
        now = datetime.now(timezone.utc)
        async with self._lock:
            for symbol in set(book_tickers) | set(mark_prices):
                state = self._states.setdefault(symbol.upper(), _SymbolMarketState())
                book_ticker = book_tickers.get(symbol)
                mark_price = mark_prices.get(symbol)
                if book_ticker is not None:
                    state.book_ticker = book_ticker
                if mark_price is not None:
                    state.mark_price = mark_price
                state.last_updated_at = now

                metrics = self._book_metrics(state.book_ticker)
                if metrics is None:
                    continue
                _bid, _ask, _bid_qty, _ask_qty, mid, spread_bps = metrics
                touch_notional_usdt = self._touch_notional_usdt(state.book_ticker)
                if touch_notional_usdt is not None:
                    state.book_history.append((now, mid, spread_bps, touch_notional_usdt))
                cutoff = now - self.BOOK_HISTORY_WINDOW
                while state.book_history and state.book_history[0][0] < cutoff:
                    state.book_history.popleft()
                book_stable, _reasons, _touch, _velocity, _mark_gap = self._book_stability_assessment(
                    state,
                    book_ticker=state.book_ticker,
                    mark_price=state.mark_price,
                )
                if not book_stable:
                    continue
                if (
                    state.last_sampled_at is not None
                    and (now - state.last_sampled_at).total_seconds() < self.HISTORY_SAMPLE_SECONDS
                ):
                    continue
                state.last_sampled_at = now
                state.spread_history.append((now, spread_bps))
                cutoff = now - self.HISTORY_WINDOW
                while state.spread_history and state.spread_history[0][0] < cutoff:
                    state.spread_history.popleft()
                state.spread_median_bps = median(value for _, value in state.spread_history) if state.spread_history else None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._refresh_once()
            except Exception as exc:
                logger.warning("market_health.refresh_failed", error=str(exc))
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.POLL_SECONDS)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        self._stop = asyncio.Event()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def snapshot(
        self,
        symbol: str,
        *,
        fallback_book_ticker: dict | None = None,
        fallback_mark_price: dict | None = None,
    ) -> MarketHealthSnapshot:
        normalized_symbol = symbol.upper()
        async with self._lock:
            state = self._states.get(normalized_symbol)
            book_ticker = state.book_ticker if state and state.book_ticker is not None else fallback_book_ticker
            mark_price = state.mark_price if state and state.mark_price is not None else fallback_mark_price
            spread_bps = self._spread_bps(book_ticker)
            spread_median_bps = state.spread_median_bps if state is not None else None
            relative_spread_sample_count = len(state.spread_history) if state is not None else 0
            relative_spread_ready = relative_spread_sample_count >= self.MIN_RELATIVE_SPREAD_SAMPLES and spread_median_bps is not None and spread_median_bps > 0
            spread_relative_ratio = (
                (spread_bps / spread_median_bps)
                if relative_spread_ready and spread_bps is not None and spread_median_bps is not None and spread_median_bps > 0
                else None
            )
            book_state = state if state is not None else _SymbolMarketState()
            book_stable, stability_reasons, touch_notional_usdt, quote_velocity_bps, mark_gap_bps = self._book_stability_assessment(
                book_state,
                book_ticker=book_ticker,
                mark_price=mark_price,
            )
            return MarketHealthSnapshot(
                symbol=normalized_symbol,
                book_ticker=book_ticker,
                mark_price=mark_price,
                spread_bps=spread_bps,
                spread_median_bps=spread_median_bps,
                spread_relative_ratio=spread_relative_ratio,
                relative_spread_ready=relative_spread_ready,
                relative_spread_sample_count=relative_spread_sample_count,
                book_stable=book_stable,
                book_stability_reasons=stability_reasons,
                touch_notional_usdt=touch_notional_usdt,
                quote_velocity_bps=quote_velocity_bps,
                mark_gap_bps=mark_gap_bps,
                last_updated_at=state.last_updated_at if state is not None else None,
            )
