from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.models.aqrr_trade_stat import AqrrTradeStat
from app.models.enums import OrderStatus, SignalDirection


MIN_BUCKET_SAMPLE_SIZE = 20


@dataclass(frozen=True)
class CandidateStatsBucket:
    bucket_key: str
    setup_family: str
    direction: SignalDirection
    market_state: str
    score_band: str
    volatility_band: str
    execution_tier: str


def score_band_for_final_score(final_score: int) -> str:
    if final_score >= 90:
        return "90_plus"
    if final_score >= 80:
        return "80_89"
    if final_score >= 70:
        return "70_79"
    return "below_70"


def volatility_band_for_percentile(atr_percentile: float | None) -> str:
    if atr_percentile is None:
        return "unknown"
    if atr_percentile <= 0.33:
        return "compressed"
    if atr_percentile <= 0.66:
        return "normal"
    if atr_percentile <= 0.97:
        return "expanded"
    return "extreme"


def build_candidate_stats_bucket(
    *,
    setup_family: str,
    direction: SignalDirection,
    market_state: str,
    execution_tier: str,
    final_score: int,
    atr_percentile: float | None,
) -> CandidateStatsBucket:
    score_band = score_band_for_final_score(final_score)
    volatility_band = volatility_band_for_percentile(atr_percentile)
    bucket_key = "|".join(
        (
            setup_family,
            direction.value,
            market_state,
            score_band,
            volatility_band,
            execution_tier,
        )
    )
    return CandidateStatsBucket(
        bucket_key=bucket_key,
        setup_family=setup_family,
        direction=direction,
        market_state=market_state,
        score_band=score_band,
        volatility_band=volatility_band,
        execution_tier=execution_tier,
    )


def hit_rate_score(stat: AqrrTradeStat | None) -> float | None:
    if stat is None or stat.closed_trade_count < MIN_BUCKET_SAMPLE_SIZE:
        return None
    return (stat.win_count / stat.closed_trade_count) * 100.0


def calibrated_rank_value(*, final_score: int, stat: AqrrTradeStat | None) -> tuple[float, float | None]:
    hit_rate = hit_rate_score(stat)
    if hit_rate is None:
        return float(final_score), None
    return (0.70 * float(final_score)) + (0.30 * hit_rate), hit_rate


async def load_trade_stats(session, *, bucket_keys: list[str]) -> dict[str, AqrrTradeStat]:
    if not bucket_keys:
        return {}
    rows = (
        await session.execute(
            select(AqrrTradeStat).where(AqrrTradeStat.bucket_key.in_(bucket_keys))
        )
    ).scalars().all()
    return {row.bucket_key: row for row in rows}


async def record_closed_trade_stat(
    session,
    *,
    bucket: CandidateStatsBucket | None,
    closed_status: OrderStatus,
    closed_at,
) -> None:
    if bucket is None or closed_status not in {OrderStatus.CLOSED_WIN, OrderStatus.CLOSED_LOSS}:
        return

    stat = await session.get(AqrrTradeStat, bucket.bucket_key)
    if stat is None:
        stat = AqrrTradeStat(
            bucket_key=bucket.bucket_key,
            setup_family=bucket.setup_family,
            direction=bucket.direction,
            market_state=bucket.market_state,
            score_band=bucket.score_band,
            volatility_band=bucket.volatility_band,
            execution_tier=bucket.execution_tier,
            closed_trade_count=0,
            win_count=0,
            loss_count=0,
        )
        session.add(stat)

    stat.closed_trade_count += 1
    if closed_status == OrderStatus.CLOSED_WIN:
        stat.win_count += 1
    else:
        stat.loss_count += 1
    stat.last_closed_at = closed_at
