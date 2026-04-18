"""
tests/test_validation_roundtrip.py

Pure offline, test-only end-to-end round-trip for the walk-forward artifact pipeline.

What this proves:
  1. A walk-forward artifact can be generated from mock SliceResults with sim_outcomes.
  2. The artifact structure is valid (Pydantic schema gate).
  3. The artifact is compatible with the existing import_walk_forward_buckets() schema path.
  4. Expected bucket stats (win_count, loss_count, closed_trade_count, keys) are correct
     after a simulated import, verified through a test-only in-memory session.

Constraints honoured:
  - Does NOT modify aqrr_trade_stat.py (ORM model).
  - Does NOT modify statistics.py (runtime import hook).
  - Does NOT require Alembic migrations.
  - Does NOT change the live DB schema.
  - Uses a test-only FakeSession that exactly mirrors the AqrrTradeStat fields
    that the existing import_walk_forward_buckets() actually writes to.
"""

from __future__ import annotations

import pytest

from app.models.enums import SignalDirection
from app.schemas.validation import BucketExportArtifact, SliceResult
from app.services.strategy.statistics import (
    build_candidate_stats_bucket,
    import_walk_forward_buckets,
)
from scripts.walkforward_runner import build_bucket_export


# ---------------------------------------------------------------------------
# Test-only FakeSession — mirrors only the runtime fields that
# import_walk_forward_buckets() actually reads/writes.
# This avoids any ORM model or DB schema changes.
# ---------------------------------------------------------------------------

class _FakeTradeStat:
    """
    In-memory bucket record.  Mirrors only the fields that
    import_walk_forward_buckets() actually writes to an AqrrTradeStat row:
        bucket_key, setup_family, direction, market_state,
        score_band, volatility_band, execution_tier,
        closed_trade_count, win_count, loss_count, last_closed_at.
    No new fields are added to the real ORM model.
    """
    __slots__ = (
        "bucket_key", "setup_family", "direction", "market_state",
        "score_band", "volatility_band", "execution_tier",
        "closed_trade_count", "win_count", "loss_count", "last_closed_at",
    )

    def __init__(self, **kwargs):
        for field in self.__slots__:
            setattr(self, field, kwargs.get(field))


class FakeSession:
    """
    Drop-in async test double for SQLAlchemy AsyncSession.
    Implements only the API surface used by import_walk_forward_buckets():
      - await session.get(Model, primary_key)
      - session.add(instance)
    No real database, no real ORM, no connection strings.
    """

    def __init__(self):
        self._store: dict[str, _FakeTradeStat] = {}
        self.committed = False

    async def get(self, model, primary_key: str):
        # model is AqrrTradeStat at runtime — we ignore the class and just
        # look up by primary key string, which is all the hook needs.
        return self._store.get(primary_key)

    def add(self, instance):
        # Store the real object reference — the import hook mutates win_count /
        # loss_count / closed_trade_count AFTER calling session.add(), so we
        # must keep the live object, not a snapshot copy.
        self._store[instance.bucket_key] = instance

    async def commit(self):
        self.committed = True

    def records(self) -> list[_FakeTradeStat]:
        return list(self._store.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_slice(candidates: list[dict]) -> SliceResult:
    return SliceResult(
        period_index=0, train_start=0, train_end=1,
        test_start=1, test_end=2, symbol="BTCUSDT",
        total_steps=len(candidates), steps_skipped=0,
        steps_with_candidates=len(candidates),
        candidates=candidates,
    )


def _candidate(sim_outcome: str, net_r_multiple: float = 1.0) -> dict:
    return {
        "symbol": "BTCUSDT", "direction": "LONG",
        "setup_family": "trend_breakout",
        "setup_variant": "v1", "entry_style": "LIMIT",
        "entry_price": 100, "stop_loss": 90, "take_profit": 110,
        "net_r_multiple": net_r_multiple,
        "final_score": 85, "market_state": "bull",
        "execution_tier": "TIER_A", "atr_percentile": 0.5,
        "expiry_bars": 3, "sim_outcome": sim_outcome,
    }


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_artifact_generation_and_schema_validation():
    """
    Step 1+2: walk-forward output is produced correctly and passes schema.
    """
    slice_res = _make_slice([
        _candidate("WIN"), _candidate("LOSS"), _candidate("UNRESOLVED"),
    ])
    artifact = build_bucket_export([slice_res])

    # Validates the Pydantic schema gate
    assert isinstance(artifact, BucketExportArtifact)
    assert len(artifact.buckets) == 1

    b = artifact.buckets[0]
    assert b.candidate_count == 3
    assert b.closed_trade_count == 2
    assert b.win_count == 1
    assert b.loss_count == 1
    assert b.gross_profit == 1.0
    assert b.gross_loss == 1.0


@pytest.mark.asyncio
async def test_artifact_import_compatibility_with_existing_hook():
    """
    Step 3+4: Artifact passes through the unmodified import_walk_forward_buckets()
    hook and the resulting in-memory records match expected stats.
    """
    slice_res = _make_slice([
        _candidate("WIN", net_r_multiple=2.0),
        _candidate("WIN", net_r_multiple=2.0),
        _candidate("LOSS"),
    ])
    artifact = build_bucket_export([slice_res])

    # model_dump simulates the JSON serialisation boundary (file → hook)
    payload = artifact.model_dump()

    session = FakeSession()
    # Call the unmodified runtime import hook
    await import_walk_forward_buckets(session, export_payload=payload)
    await session.commit()

    assert session.committed is True
    recs = session.records()
    assert len(recs) == 1

    stat = recs[0]
    assert stat.closed_trade_count == 3
    assert stat.win_count == 2
    assert stat.loss_count == 1
    assert stat.setup_family == "trend_breakout"
    assert stat.direction == SignalDirection.LONG
    assert stat.score_band == "80_89"
    assert stat.volatility_band == "normal"


@pytest.mark.asyncio
async def test_bucket_key_round_trips_correctly():
    """
    Proves that the key produced by build_bucket_export matches the key that
    import_walk_forward_buckets() will reconstruct internally — no silent drift.
    """
    slice_res = _make_slice([_candidate("WIN")])
    artifact = build_bucket_export([slice_res])
    payload = artifact.model_dump()

    session = FakeSession()
    await import_walk_forward_buckets(session, export_payload=payload)

    # Also derive the expected key independently via the canonical helper
    expected = build_candidate_stats_bucket(
        setup_family="trend_breakout",
        direction=SignalDirection.LONG,
        market_state="bull",
        execution_tier="tier_a",
        final_score=85,
        atr_percentile=0.5,
    )

    recs = session.records()
    assert len(recs) == 1
    assert recs[0].bucket_key == expected.bucket_key


@pytest.mark.asyncio
async def test_import_override_replaces_not_appends():
    """
    A second import for the same bucket key must override, not duplicate.
    Exercises the existing runtime hook's upsert path.
    """
    session = FakeSession()

    first = BucketExportArtifact(
        generated_at=1000, walk_forward_range_start=0, walk_forward_range_end=1,
        data_points=1, buckets=[{
            "setup_family": "trend_breakout", "direction": SignalDirection.LONG,
            "market_state": "bull", "score_band": "80_89", "volatility_band": "normal",
            "execution_tier": "tier_a",
            "closed_trade_count": 5, "win_count": 3, "loss_count": 2,
            "breakeven_count": 0, "gross_profit": 3.0, "gross_loss": 2.0,
            "candidate_count": 5,
        }]
    )
    await import_walk_forward_buckets(session, export_payload=first.model_dump())

    assert session.records()[0].win_count == 3

    second = BucketExportArtifact(
        generated_at=2000, walk_forward_range_start=0, walk_forward_range_end=1,
        data_points=1, buckets=[{
            "setup_family": "trend_breakout", "direction": SignalDirection.LONG,
            "market_state": "bull", "score_band": "80_89", "volatility_band": "normal",
            "execution_tier": "tier_a",
            "closed_trade_count": 20, "win_count": 17, "loss_count": 3,
            "breakeven_count": 0, "gross_profit": 17.0, "gross_loss": 3.0,
            "candidate_count": 20,
        }]
    )
    await import_walk_forward_buckets(session, export_payload=second.model_dump())

    recs = session.records()
    assert len(recs) == 1, "Must not duplicate — same bucket key must be updated in place"
    assert recs[0].win_count == 17
    assert recs[0].closed_trade_count == 20


@pytest.mark.asyncio
async def test_unresolved_candidates_excluded_from_closed_count():
    """
    UNRESOLVED and EXPIRED_NO_FILL candidates must not inflate closed_trade_count.
    """
    slice_res = _make_slice([
        _candidate("WIN"),
        _candidate("UNRESOLVED"),
        _candidate("EXPIRED_NO_FILL"),
    ])
    artifact = build_bucket_export([slice_res])

    b = artifact.buckets[0]
    assert b.candidate_count == 3
    assert b.closed_trade_count == 1  # Only WIN counts as a resolved trade
    assert b.win_count == 1
    assert b.loss_count == 0
