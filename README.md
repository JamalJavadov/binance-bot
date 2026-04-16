# Binance Futures Pending-Order Bot

Local full-stack trading bot for Binance USD-M Futures. The backend is FastAPI with SQLAlchemy async/PostgreSQL. The frontend is React + TypeScript + Vite and connects to the backend over REST and WebSocket.

## Stack

- Python 3.14
- FastAPI, SQLAlchemy async, Alembic, APScheduler, httpx, structlog
- React 19, TypeScript, Vite, Zustand, Axios, React Router 7
- Local PostgreSQL

## Quick Start

1. Copy `.env.example` to `.env` and update the database credentials.
2. Create the database and user in your local PostgreSQL instance, and make sure PostgreSQL is already running.
3. Activate the project:

```bash
./start.sh
```

`start.sh` will:

- verify `python3`, `npm`, `curl`, and `lsof`
- verify `.env` exists
- validate `.env` values before migrations
- create `backend/.venv` if needed
- install backend and frontend dependencies when manifests changed
- verify PostgreSQL connectivity using the credentials from `.env`
- run `alembic upgrade head`
- start backend and frontend in the background
- write PID/state files under `.run/`
- write logs to `logs/backend.log` and `logs/frontend.log`
- wait for both services to become healthy
- open `http://localhost:3000`

Wait for `[start] Project is ready.` before assuming the app is available. If you press `Ctrl-C` after the backend and frontend have been spawned, `start.sh` now detaches and leaves them running in the background; use the printed URLs and stop them later with `./stop.sh`.

4. Deactivate the project when you are done:

```bash
./stop.sh
```

`stop.sh` will stop only the backend and frontend processes started by this project and clean `.run/` runtime metadata.

## Backend

- REST endpoints are under `/api`
- WebSocket endpoint is `/ws`
- Alembic migrations live in `backend/alembic`

## Notes

- Binance is configured for live USD-M Futures only.
- Auto Mode is disabled by default.
- Credentials are stored locally in PostgreSQL as requested by the spec.
- AQRR scans the full USD-M eligible universe and applies liquidity, spread, regime, and diversification filters from `AQRR_Binance_USDM_Strategy_Spec.md`.
- Auto Mode uses up to 3 shared entry slots across pending entries and open positions; it does not force all 3 slots to be filled every cycle.
- Runtime PID/status files live in `.run/`.
- `stop.sh` does not remove `.env`, the database, `backend/.venv`, `frontend/node_modules`, or logs.
- Frontend API base URL can be overridden with `VITE_API_BASE_URL`; if omitted, local dev falls back to `http://127.0.0.1:8000/api`.
- The project now exposes only one trading workflow and one strategy: `AUTO_MODE` with `aqrr_binance_usdm` (`AQRR Binance USD-M Strategy`). See `AQRR_Binance_USDM_Strategy_Spec.md`.

## AQRR Validation

The repo now exposes a visible AQRR validation ladder:

- `python backend/scripts/aqrr_validation.py backtest`
- `python backend/scripts/aqrr_validation.py walk-forward`
- `python backend/scripts/aqrr_validation.py paper`
- `python backend/scripts/aqrr_validation.py micro-live`

Reference docs:

- `docs/AQRR_VALIDATION_LADDER.md`
- `docs/AQRR_MICRO_LIVE_READINESS.md`

## Troubleshooting

- `DATABASE_URL` must use the async format:

```bash
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/dbname
```

- The backend uses `asyncpg` at runtime, but Alembic migrations run through SQLAlchemy's sync PostgreSQL path. Keep backend dependencies installed from `backend/requirements.txt` so the sync driver (`psycopg2-binary`) is available when `alembic upgrade head` runs.

- `CORS_ORIGINS` should stay a simple comma-separated string:

```bash
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

- To point the frontend at a non-default backend base URL:

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000/api
```

- `.env` is a hidden file on macOS Finder and `ls` by default. To see it in Terminal:

```bash
ls -la
```
