# AQRR Micro-Live Readiness

Use this checklist before enabling AQRR with real capital, even at micro size.

## Exchange and Account

- Confirm Binance USD-M Futures is in one-way mode.
- Confirm isolated margin is available for the target symbol set.
- Confirm live commission lookup or configured fee overrides match the account tier.
- Confirm leverage bracket data is reachable for the intended symbols.

## AQRR Execution Safety

- Confirm deployable equity uses 90% of account equity, not full available balance.
- Confirm per-slot sizing is equal across remaining opportunity slots.
- Confirm stop-risk includes entry fee, exit fee, and slippage burden.
- Confirm liquidation safety rejects trades whose stop is too close to the bracket-aware liquidation estimate.
- Confirm unstable-book rejection is active for thin, erratic, or mark-divergent books.

## Supervision

- Confirm `ORDER_TRADE_UPDATE` wakes lifecycle supervision immediately.
- Confirm `ACCOUNT_UPDATE` refreshes cached account state and prioritizes reconciliation.
- Confirm degraded stream health is visible and does not masquerade as normal operation.
- Confirm polling fallback remains available when the primary stream is down.

## Fill and Rejection Handling

- Confirm a too-small partial fill is closed instead of left orphaned.
- Confirm exchange filter rejects record the reason and allow at most one safe recalculation attempt.
- Confirm a second invalid recalculation result rejects the trade cleanly.

## Artifacts to Review

- Review `ORDER_SUBMISSION_RECALCULATED` and `ORDER_SUBMISSION_RECALC_REJECTED` audit events.
- Review `ORDER_MINIMUM_VIABLE_FILL_CLOSED` audit events.
- Review the latest scan diagnostics under `logs/diagnostic_scan.log`.
