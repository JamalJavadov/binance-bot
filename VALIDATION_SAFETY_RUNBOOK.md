# AQRR Validation Safety Runbook

This runbook acts as the primary instruction manual for offline strategy validation execution. 

> [!CAUTION]
> **Production Boundary Warning:**
> Offline validation simulations and database imports MUST NEVER be conducted against your live production database. Running validation DB bridges on live configurations will permanently pollute the auto-mode hit-rate scaling and trigger unwarranted positional trades.

## 1. Setting Up The Isolated Env
Before executing offline imports, enforce environment isolation.

Create or copy an isolated `.env` configuration template pointing to a database name containing the word `validation` or `test`:
```bash
cp backend/.env.validation.example backend/.env.validation
export $(cat backend/.env.validation | xargs)
```

## 2. Validation CLI Pipeline
The data preparation and importing bridge executes locally across four manual CLI scripts.

### Step 1: Historical Fetcher
Downloads Binance K-Lines (OHLCV) without accessing the system DB.
```bash
python backend/scripts/historical_fetch.py \
  --symbol BTCUSDT \
  --interval 15m \
  --start 1672531200000 \
  --end 1680307200000 \
  --outdir data/historical
```

### Step 2: Configuration Slicing & Prep
Loads your JSON validation schema and synthesizes an empty out-of-sample data artifact.
```bash
python backend/scripts/walkforward_prep.py \
  --config data/validation_config_example.json \
  --output data/validation/artifact_template.json
```

### Step 3: Mock Sandbox Generation (Development)
Generates an artificial bucket schema output envelope used to test import connections while the true backtesting loop remains in development.
```bash
python backend/scripts/walkforward_mock_gen.py \
  --output data/validation/mock_walkforward_artifact.json
```

### Step 4: Import Bridging (Requires Safety Flags)
Writes the offline-generated metric slices into the local Postgres statistics tables for live `statistics.py` ranking execution. 
**This command will crash immediately unless `--confirm-nonprod` is passed and `DATABASE_URL` contains 'test/validation'.**
```bash
python backend/scripts/walkforward_import_test.py \
  --input data/validation/mock_walkforward_artifact.json \
  --confirm-nonprod
```
