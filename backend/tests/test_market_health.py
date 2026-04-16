from __future__ import annotations

import pytest

from app.services.market_health import MarketHealthService


class SequenceGateway:
    def __init__(self, *, book_snapshots: list[dict[str, dict]], mark_snapshots: list[dict[str, dict]]) -> None:
        self.book_snapshots = list(book_snapshots)
        self.mark_snapshots = list(mark_snapshots)

    async def book_tickers(self) -> dict[str, dict]:
        return self.book_snapshots.pop(0)

    async def mark_prices(self) -> dict[str, dict]:
        return self.mark_snapshots.pop(0)


@pytest.mark.asyncio
async def test_market_health_keeps_stable_book_quotes_tradable() -> None:
    service = MarketHealthService(
        SequenceGateway(
            book_snapshots=[
                {"BTCUSDT": {"bidPrice": "100.00", "askPrice": "100.02", "bidQty": "5", "askQty": "5"}},
                {"BTCUSDT": {"bidPrice": "100.01", "askPrice": "100.03", "bidQty": "5", "askQty": "5"}},
                {"BTCUSDT": {"bidPrice": "100.02", "askPrice": "100.04", "bidQty": "5", "askQty": "5"}},
            ],
            mark_snapshots=[
                {"BTCUSDT": {"markPrice": "100.01"}},
                {"BTCUSDT": {"markPrice": "100.02"}},
                {"BTCUSDT": {"markPrice": "100.03"}},
            ],
        )
    )

    await service._refresh_once()
    await service._refresh_once()
    await service._refresh_once()
    snapshot = await service.snapshot("BTCUSDT")

    assert snapshot.book_stable is True
    assert snapshot.book_stability_reasons == ()
    assert snapshot.touch_notional_usdt is not None


@pytest.mark.asyncio
async def test_market_health_rejects_erratic_quote_movement_even_when_book_is_not_crossed() -> None:
    service = MarketHealthService(
        SequenceGateway(
            book_snapshots=[
                {"BTCUSDT": {"bidPrice": "100.00", "askPrice": "100.02", "bidQty": "5", "askQty": "5"}},
                {"BTCUSDT": {"bidPrice": "100.80", "askPrice": "100.82", "bidQty": "5", "askQty": "5"}},
                {"BTCUSDT": {"bidPrice": "99.60", "askPrice": "99.62", "bidQty": "5", "askQty": "5"}},
            ],
            mark_snapshots=[
                {"BTCUSDT": {"markPrice": "100.01"}},
                {"BTCUSDT": {"markPrice": "100.05"}},
                {"BTCUSDT": {"markPrice": "100.00"}},
            ],
        )
    )

    await service._refresh_once()
    await service._refresh_once()
    await service._refresh_once()
    snapshot = await service.snapshot("BTCUSDT")

    assert snapshot.book_stable is False
    assert "erratic_quote_movement" in snapshot.book_stability_reasons or "book_mark_divergence" in snapshot.book_stability_reasons


@pytest.mark.asyncio
async def test_market_health_rejects_thin_touch_liquidity_as_unstable_book() -> None:
    service = MarketHealthService(
        SequenceGateway(
            book_snapshots=[
                {"BTCUSDT": {"bidPrice": "100.00", "askPrice": "100.02", "bidQty": "0.01", "askQty": "0.01"}},
            ],
            mark_snapshots=[
                {"BTCUSDT": {"markPrice": "100.01"}},
            ],
        )
    )

    await service._refresh_once()
    snapshot = await service.snapshot("BTCUSDT")

    assert snapshot.spread_bps is not None
    assert snapshot.book_stable is False
    assert snapshot.book_stability_reasons == ("touch_liquidity_thin",)
