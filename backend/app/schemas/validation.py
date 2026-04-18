from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import SignalDirection


class WalkForwardPeriod(BaseModel):
    train_start: int = Field(description="Start timestamp in milliseconds for training period")
    train_end: int = Field(description="End timestamp in milliseconds for training period")
    test_start: int = Field(description="Start timestamp in milliseconds for test period")
    test_end: int = Field(description="End timestamp in milliseconds for test period")


class WalkForwardConfig(BaseModel):
    symbols: list[str] = Field(description="List of symbols to evaluate")
    intervals: list[Literal["15m", "1h", "4h"]] = Field(description="Candle intervals required")
    periods: list[WalkForwardPeriod] = Field(description="Sequential walk-forward slicing windows")
    min_bucket_samples: int = Field(default=20, description="Minimum trades before ranking relies on bucket stat")


class BucketGeneratedStat(BaseModel):
    setup_family: str
    direction: SignalDirection
    market_state: str
    score_band: str
    volatility_band: str
    execution_tier: str
    closed_trade_count: int
    win_count: int
    loss_count: int
    breakeven_count: int
    gross_profit: float
    gross_loss: float
    # candidate_count tracks how many times this bucket was triggered during
    # the walk-forward test window. Distinct from closed_trade_count, which
    # requires a full execution simulator (Phase 3+).
    candidate_count: int = 0

    @property
    def hit_rate(self) -> float:
        if self.closed_trade_count == 0:
            return 0.0
        return (self.win_count / self.closed_trade_count) * 100.0


class BucketExportArtifact(BaseModel):
    generated_at: int = Field(description="Generation timestamp ms")
    walk_forward_range_start: int
    walk_forward_range_end: int
    data_points: int
    buckets: list[BucketGeneratedStat]
    # Describes what is and is not present in this artifact
    phase_notes: list[str] = Field(default_factory=list)


class SliceResult(BaseModel):
    """
    The candidate-level output for one walk-forward test window (one period).
    Produced by the walk-forward runner; consumed by the bucket export pipeline.
    """
    period_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    symbol: str
    total_steps: int
    steps_skipped: int
    steps_with_candidates: int
    # Flat list of all candidates found during the test window
    candidates: list[dict]
