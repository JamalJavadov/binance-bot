# AQRR Validation Ladder

This repository exposes the minimum visible AQRR validation path required before scaling live capital.

## 1. Backtest

Run the scaffold entry point:

```bash
python backend/scripts/aqrr_validation.py backtest --symbol BTCUSDT
```

Expected outcome:

- confirm the runner is wired,
- point the run at the historical dataset/config you intend to use,
- record output under a reproducible report path.

## 2. Walk-Forward

Run the scaffold entry point:

```bash
python backend/scripts/aqrr_validation.py walk-forward --input data/walk_forward_config.json
```

Expected outcome:

- validate sequential train/test slices,
- preserve AQRR ranking, sizing, and execution assumptions,
- record per-window results before any paper or live step.

## 3. Paper Trading

Run the scaffold entry point:

```bash
python backend/scripts/aqrr_validation.py paper --output reports/paper
```

Expected outcome:

- verify scanner to order lifecycle behavior without live capital,
- confirm user-stream supervision, partial-fill handling, and exchange-filter rejection logging,
- review order and audit artifacts before micro-live.

## 4. Micro-Live Readiness

Run the scaffold entry point:

```bash
python backend/scripts/aqrr_validation.py micro-live
```

Then complete the checklist in [`AQRR_MICRO_LIVE_READINESS.md`](./AQRR_MICRO_LIVE_READINESS.md).

Expected outcome:

- confirm deployable-equity sizing,
- confirm fee-inclusive stop risk,
- confirm bracket-aware liquidation safety,
- confirm event-driven supervision and fallback health behavior,
- confirm minimum-viable fill cleanup and exchange-reject recalculation logging.
