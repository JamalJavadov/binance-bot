"""
app.validation

Shared offline validation utilities.

This package contains pure, stateless helpers used by the offline
backtest runner and walk-forward pipeline. It has NO imports from
the live runtime (no gateway, no DB session, no order manager).
"""
