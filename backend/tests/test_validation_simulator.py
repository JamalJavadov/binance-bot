"""
test_validation_simulator.py

Tests for the minimal offline execution simulator.
Ensures deterministic deterministic execution boundaries for Entry expiration
and StopLoss/TakeProfit interactions, guaranteeing strict offline validation fidelity.
"""

from __future__ import annotations

import pytest

from app.models.enums import SignalDirection
from app.services.strategy.types import Candle
from app.validation.simulator import SimOutcome, simulate_execution


def _make_candle(open_time: int, low: float, high: float, close: float = 100.0) -> Candle:
    return Candle(
        open_time=open_time,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1000.0
    )


class TestValidationSimulator:
    
    def test_simulator_long_limit_take_profit_first(self) -> None:
        """
        Setup triggers on a dip (LIMIT LONG), subsequently rises and hits TP first.
        """
        future_candles = [
            _make_candle(1000, 102.0, 104.0),  # Not filled (low > entry)
            _make_candle(2000, 99.0, 103.0),   # Filled (low <= 100)
            _make_candle(3000, 101.0, 109.0),  # Progresses upwards
            _make_candle(4000, 105.0, 115.0),  # Hits TP (high >= 110)
        ]
        
        result = simulate_execution(
            direction=SignalDirection.LONG,
            entry_style="LIMIT_GTD",
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            expiry_bars=5,
            future_candles=future_candles
        )
        
        assert result.outcome == SimOutcome.WIN
        assert result.fill_time_ms == 2000
        assert result.exit_time_ms == 4000

    def test_simulator_short_stop_loss_first(self) -> None:
        """
        Setup triggers on a breakdown (STOP SHORT), subsequently reverses hitting SL first.
        """
        future_candles = [
            _make_candle(1000, 101.0, 103.0),  # Not filled (low > entry)
            _make_candle(2000, 99.0, 102.0),   # Filled (low <= 100)
            _make_candle(3000, 98.0, 101.0),   # Moves in favor slightly
            _make_candle(4000, 100.0, 106.0),  # Spikes and hits SL (high >= 105)
        ]
        
        result = simulate_execution(
            direction=SignalDirection.SHORT,
            entry_style="STOP_ENTRY",
            entry_price=100.0,
            stop_loss=105.0,
            take_profit=90.0,
            expiry_bars=5,
            future_candles=future_candles
        )
        
        assert result.outcome == SimOutcome.LOSS
        assert result.fill_time_ms == 2000
        assert result.exit_time_ms == 4000

    def test_simulator_conservative_intrabar_loss(self) -> None:
        """
        A single candle exhibits massive volatility, touching both SL and TP.
        Simulator must conservatively register a LOSS.
        """
        future_candles = [
            _make_candle(1000, 99.0, 101.0),   # Filled LONG (low <= 100.0)
            _make_candle(2000, 80.0, 120.0),   # Exact same candle hits both SL(90.0) and TP(110.0)
        ]
        
        result = simulate_execution(
            direction=SignalDirection.LONG,
            entry_style="LIMIT_GTD",
            entry_price=100.0,
            stop_loss=90.0,
            take_profit=110.0,
            expiry_bars=5,
            future_candles=future_candles
        )
        
        assert result.outcome == SimOutcome.LOSS
        assert result.fill_time_ms == 1000
        assert result.exit_time_ms == 2000

    def test_simulator_expiry_no_fill(self) -> None:
        """
        Setup never hits the target LIMIT or STOP point within expiry window.
        """
        future_candles = [
            _make_candle(1000, 105.0, 110.0), 
            _make_candle(2000, 106.0, 112.0), 
            _make_candle(3000, 104.0, 109.0), 
            _make_candle(4000, 99.0,  105.0), # Would fill here, but it's candle #4...
        ]
        
        result = simulate_execution(
            direction=SignalDirection.LONG,
            entry_style="LIMIT_GTD",
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=115.0,
            expiry_bars=3, # ...but expiry was 3 bars
            future_candles=future_candles
        )
        
        assert result.outcome == SimOutcome.EXPIRED_NO_FILL
        assert result.fill_time_ms is None
        assert result.exit_time_ms is None

    def test_simulator_unresolved_exhaustion(self) -> None:
        """
        Setup fills, but the historical data slice runs out before hitting SL or TP.
        """
        future_candles = [
            _make_candle(1000, 99.0, 101.0), # Filled
            _make_candle(2000, 98.0, 102.0), # Active
            _make_candle(3000, 97.0, 103.0), # Active
        ]
        
        result = simulate_execution(
            direction=SignalDirection.LONG,
            entry_style="LIMIT_GTD",
            entry_price=100.0,
            stop_loss=90.0,
            take_profit=110.0,
            expiry_bars=5,
            future_candles=future_candles
        )
        
        assert result.outcome == SimOutcome.UNRESOLVED
        assert result.fill_time_ms == 1000
        assert result.exit_time_ms is None
