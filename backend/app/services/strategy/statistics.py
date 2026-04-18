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


async def import_walk_forward_buckets(session, *, export_payload: dict) -> None:
    """
    Stateless import boundary receiving the validated Walk-Forward bucket stats.
    Replaces historical hit-rate buckets for live scanner calibration.
    Leaves live auto-mode trading code entirely unmodified.
    """
    from datetime import datetime, timezone
    from app.schemas.validation import BucketExportArtifact

    # Validate structure and boundary constraints
    artifact = BucketExportArtifact.model_validate(export_payload)

    for bucket_stat in artifact.buckets:
        # Use the canonical helper to derive the bucket key — this is the only
        # correct construction path and matches the live scanner exactly.
        canonical = build_candidate_stats_bucket(
            setup_family=bucket_stat.setup_family,
            direction=bucket_stat.direction,
            market_state=bucket_stat.market_state,
            execution_tier=bucket_stat.execution_tier,
            final_score=_score_for_band(bucket_stat.score_band),
            atr_percentile=_percentile_for_band(bucket_stat.volatility_band),
        )

        stat = await session.get(AqrrTradeStat, canonical.bucket_key)
        if stat is None:
            stat = AqrrTradeStat(
                bucket_key=canonical.bucket_key,
                setup_family=canonical.setup_family,
                direction=canonical.direction,
                market_state=canonical.market_state,
                score_band=canonical.score_band,
                volatility_band=canonical.volatility_band,
                execution_tier=canonical.execution_tier,
                closed_trade_count=0,
                win_count=0,
                loss_count=0,
            )
            session.add(stat)

        # Walk-forward stats completely override local historical state where applicable
        stat.closed_trade_count = bucket_stat.closed_trade_count
        stat.win_count = bucket_stat.win_count
        stat.loss_count = bucket_stat.loss_count
        stat.last_closed_at = datetime.now(timezone.utc)


def _score_for_band(band: str) -> int:
    """Return a representative score for a score_band string (for canonical key derivation)."""
    return {"90_plus": 90, "80_89": 80, "70_79": 70}.get(band, 60)


def _percentile_for_band(band: str) -> float | None:
    """Return a representative ATR percentile for a volatility_band string."""
    return {"compressed": 0.20, "normal": 0.50, "expanded": 0.80, "extreme": 0.99}.get(band)

