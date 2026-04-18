"""
app.validation.step_evaluator

Single-bar AQRR evaluation step for the offline validation pipeline.

Wraps evaluate_symbol() with:
  - window-size guards (minimum history checks),
  - fixed placeholders for live-only inputs (spread, volume, funding),
  - bucket-key derivation for walk-forward stat aggregation.

All bucket derivation uses the canonical build_candidate_stats_bucket()
from statistics.py — no duplicate key construction logic here.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models.enums import ScanSymbolOutcome, SignalDirection
from app.services.strategy import aqrr as aqrr_module
from app.services.strategy.config import StrategyConfig
from app.services.strategy.statistics import (
    CandidateStatsBucket,
    build_candidate_stats_bucket,
    volatility_band_for_percentile,
    score_band_for_final_score,
)
from app.services.strategy.types import Candle
from app.validation.candle_loader import MIN_15M_BARS, MIN_1H_BARS, MIN_4H_BARS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Placeholder values for live-only inputs
# (real integration requires per-bar quote volume / spread / funding data)
# ---------------------------------------------------------------------------
_PLACEHOLDER_QUOTE_VOLUME = 100_000_000.0
_PLACEHOLDER_SPREAD_BPS = 5.0
_PLACEHOLDER_LIQUIDITY_FLOOR = 25_000_000.0
_PLACEHOLDER_FUNDING_RATE = 0.0
_PLACEHOLDER_AVAILABLE_BALANCE = 100.0
_PLACEHOLDER_MIN_NOTIONAL = 5.0
_PLACEHOLDER_TICK_SIZE = 0.01


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class StepCandidate:
    """Serialisable summary of a single candidate from one evaluation step."""

    __slots__ = (
        "symbol", "direction", "setup_family", "setup_variant", "entry_style",
        "entry_price", "stop_loss", "take_profit", "net_r_multiple",
        "final_score", "rank_value", "market_state", "execution_tier",
        "atr_percentile", "expiry_bars",
        "sim_outcome", "sim_fill_time_ms", "sim_exit_time_ms",
    )

    def __init__(
        self,
        *,
        symbol: str,
        direction: str,
        setup_family: str,
        setup_variant: str,
        entry_style: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        net_r_multiple: float,
        final_score: int,
        rank_value: float,
        market_state: str,
        execution_tier: str,
        atr_percentile: float | None,
        expiry_bars: int,
    ) -> None:
        self.symbol = symbol
        self.direction = direction
        self.setup_family = setup_family
        self.setup_variant = setup_variant
        self.entry_style = entry_style
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.net_r_multiple = net_r_multiple
        self.final_score = final_score
        self.rank_value = rank_value
        self.market_state = market_state
        self.execution_tier = execution_tier
        self.atr_percentile = atr_percentile
        self.expiry_bars = expiry_bars
        self.sim_outcome: str | None = None
        self.sim_fill_time_ms: int | None = None
        self.sim_exit_time_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "symbol": self.symbol,
            "direction": self.direction,
            "setup_family": self.setup_family,
            "setup_variant": self.setup_variant,
            "entry_style": self.entry_style,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "net_r_multiple": self.net_r_multiple,
            "final_score": self.final_score,
            "rank_value": self.rank_value,
            "market_state": self.market_state,
            "execution_tier": self.execution_tier,
            "atr_percentile": self.atr_percentile,
            "expiry_bars": self.expiry_bars,
        }
        if self.sim_outcome is not None:
            d["sim_outcome"] = self.sim_outcome
            d["sim_fill_time_ms"] = self.sim_fill_time_ms
            d["sim_exit_time_ms"] = self.sim_exit_time_ms
        return d

    def to_bucket(self) -> CandidateStatsBucket:
        """
        Derive the canonical bucket key for this candidate using the shared
        build_candidate_stats_bucket() function.

        This is the only place bucket keys should be constructed — prevents
        the key-format drift bug identified in the conformance audit.
        """
        direction_enum = (
            SignalDirection.LONG if self.direction == "LONG" else SignalDirection.SHORT
        )
        execution_tier_lower = self.execution_tier.lower()
        return build_candidate_stats_bucket(
            setup_family=self.setup_family,
            direction=direction_enum,
            market_state=self.market_state,
            execution_tier=execution_tier_lower,
            final_score=self.final_score,
            atr_percentile=self.atr_percentile,
        )


class StepResult:
    """Result of one evaluation step across all candidate slots."""

    __slots__ = ("eval_time_ms", "skipped", "skip_reason", "outcome", "market_state", "candidates", "filter_reasons")

    def __init__(
        self,
        *,
        eval_time_ms: int,
        skipped: bool,
        skip_reason: str | None = None,
        outcome: str | None = None,
        market_state: str | None = None,
        candidates: list[StepCandidate] | None = None,
        filter_reasons: list[str] | None = None,
    ) -> None:
        self.eval_time_ms = eval_time_ms
        self.skipped = skipped
        self.skip_reason = skip_reason
        self.outcome = outcome
        self.market_state = market_state
        self.candidates = candidates or []
        self.filter_reasons = filter_reasons or []

    def to_dict(self) -> dict[str, Any]:
        base: dict[str, Any] = {"eval_time_ms": self.eval_time_ms, "skipped": self.skipped}
        if self.skipped:
            base["reason"] = self.skip_reason
        else:
            base["outcome"] = self.outcome
            base["market_state"] = self.market_state
            base["candidates"] = [c.to_dict() for c in self.candidates]
            base["filter_reasons"] = self.filter_reasons
        return base


# ---------------------------------------------------------------------------
# Core evaluation step
# ---------------------------------------------------------------------------

def run_step(
    *,
    symbol: str,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    eval_time_ms: int,
    config: StrategyConfig,
) -> StepResult:
    """
    Run a single AQRR evaluation for one bar.

    Returns a StepResult containing all candidates found (or a skip record
    if there is insufficient history at this timestamp).
    """
    if (
        len(candles_15m) < MIN_15M_BARS
        or len(candles_1h) < MIN_1H_BARS
        or len(candles_4h) < MIN_4H_BARS
    ):
        return StepResult(
            eval_time_ms=eval_time_ms,
            skipped=True,
            skip_reason="insufficient_history",
        )

    current_price = candles_15m[-1].close

    evaluation = aqrr_module.evaluate_symbol(
        symbol=symbol,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        current_price=current_price,
        funding_rate=_PLACEHOLDER_FUNDING_RATE,
        quote_volume=_PLACEHOLDER_QUOTE_VOLUME,
        spread_bps=_PLACEHOLDER_SPREAD_BPS,
        liquidity_floor=_PLACEHOLDER_LIQUIDITY_FLOOR,
        filters_min_notional=_PLACEHOLDER_MIN_NOTIONAL,
        tick_size=_PLACEHOLDER_TICK_SIZE,
        available_balance=_PLACEHOLDER_AVAILABLE_BALANCE,
        config=config,
    )

    # Extract per-candidate data including atr_percentile from diagnostics
    atr_percentile: float | None = None
    if evaluation.diagnostic:
        raw = evaluation.diagnostic.get("atr_percentile")
        try:
            atr_percentile = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            atr_percentile = None

    candidates: list[StepCandidate] = []
    for c in evaluation.candidates:
        candidates.append(
            StepCandidate(
                symbol=c.symbol,
                direction=c.direction.value,
                setup_family=c.setup_family,
                setup_variant=c.setup_variant,
                entry_style=c.entry_style,
                entry_price=c.entry_price,
                stop_loss=c.stop_loss,
                take_profit=c.take_profit,
                net_r_multiple=c.net_r_multiple,
                final_score=c.final_score,
                rank_value=c.rank_value,
                market_state=c.market_state,
                execution_tier=c.execution_tier,
                atr_percentile=atr_percentile,
                # default 45 min = 3 bars if not explicitly populated
                expiry_bars=max(c.expiry_minutes // 15, 1) if c.expiry_minutes else 3,
            )
        )

    direction_val = evaluation.direction.value if evaluation.direction else None
    return StepResult(
        eval_time_ms=eval_time_ms,
        skipped=False,
        outcome=evaluation.outcome.value,
        market_state=str(evaluation.diagnostic.get("market_state", "UNKNOWN")),
        candidates=candidates,
        filter_reasons=list(evaluation.filter_reasons),
    )
