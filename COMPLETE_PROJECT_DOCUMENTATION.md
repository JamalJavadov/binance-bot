# Binance USD-M Futures Trading Bot — Complete Project Documentation

**Document type:** Full technical reference  
**Version:** 1.0  
**Date:** 2026-04-16  
**Status:** Read-only audit output — do not edit without re-running the audit

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Technical Stack](#3-technical-stack)
4. [System Architecture](#4-system-architecture)
5. [Startup & Operational Workflow](#5-startup--operational-workflow)
6. [Environment & Configuration](#6-environment--configuration)
7. [Database Layer](#7-database-layer)
8. [Backend Services — Detailed Reference](#8-backend-services--detailed-reference)
   - 8.1 [BinanceGateway](#81-binancegateway)
   - 8.2 [ScannerService](#82-scannerservice)
   - 8.3 [AutoModeService](#83-automodeservice)
   - 8.4 [OrderManager](#84-ordermanager)
   - 8.5 [LifecycleMonitor](#85-lifecyclemonitor)
   - 8.6 [UserDataStreamSupervisor](#86-userdatastreamsupervisor)
   - 8.7 [SchedulerService](#87-schedulerservice)
   - 8.8 [MarketHealthService](#88-markethealthservice)
   - 8.9 [PositionObserver](#89-positionobserver)
   - 8.10 [SettingsService](#810-settingsservice)
   - 8.11 [NotifierService](#811-notifierservice)
9. [AQRR Strategy Engine](#9-aqrr-strategy-engine)
   - 9.1 [Philosophy](#91-philosophy)
   - 9.2 [Market State Classification](#92-market-state-classification)
   - 9.3 [Setup Families](#93-setup-families)
   - 9.4 [Net 3R Feasibility Gate](#94-net-3r-feasibility-gate)
   - 9.5 [Quality Scoring Engine](#95-quality-scoring-engine)
   - 9.6 [Candidate Ranking & Selection](#96-candidate-ranking--selection)
   - 9.7 [Historical Bucket Calibration](#97-historical-bucket-calibration)
10. [API Layer — REST Endpoints](#10-api-layer--rest-endpoints)
11. [Data Models — SQLAlchemy](#11-data-models--sqlalchemy)
12. [Frontend Application](#12-frontend-application)
13. [Auto Mode — Full Cycle Walkthrough](#13-auto-mode--full-cycle-walkthrough)
14. [Order Lifecycle](#14-order-lifecycle)
15. [Security Model](#15-security-model)
16. [Logging & Auditability](#16-logging--auditability)
17. [Testing](#17-testing)
18. [Operational Runbook](#18-operational-runbook)
19. [Known Constraints & Risks](#19-known-constraints--risks)
20. [Validation Ladder](#20-validation-ladder)

---

## 1. Project Overview

This project is a **fully automated, local-only, single-user trading bot** for the Binance USD-M Futures market. It is designed around the **AQRR (Adaptive Quality-Ranked Regime) strategy**, which scans the full live universe of perpetual futures, classifies each symbol by market regime, scores only the best setups, and places at most three simultaneous trades.

**Core design principles:**

- **No forced trades.** If nothing qualifies, the system does nothing.
- **Quality over quantity.** A cross-sectional ranking system selects only 0–3 opportunities per scan cycle.
- **Execution-first.** Every candidate must survive a live spread check, minimum notional test, liquidation-buffer check, and net-3R cost gate before it is accepted.
- **Full automation.** From market scan → signal qualification → order placement → stop/take-profit management → closed-trade recording — the full lifecycle is automated with no human approval required.
- **Local-only / single-user.** No cloud, no multi-user auth, no SaaS assumptions. PostgreSQL and all services run on the developer's machine.

---

## 2. Repository Layout

```
BINANCE-CRYPTO-BOT/
├── AQRR_Binance_USDM_Strategy_Spec.md   # Canonical strategy specification (1 800 lines)
├── README.md
├── start.sh                              # Full-stack startup script
├── stop.sh                               # Clean shutdown script
├── .env                                  # Runtime config (gitignored)
├── .env.example                          # Template
├── pytest.ini
├── docs/
│   ├── AQRR_VALIDATION_LADDER.md
│   └── AQRR_MICRO_LIVE_READINESS.md
├── backend/
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/                          # Migration scripts
│   ├── scripts/
│   │   └── aqrr_validation.py            # Validation scaffold CLI
│   └── app/
│       ├── main.py                       # FastAPI entry point + lifespan
│       ├── core/
│       │   ├── config.py                 # Pydantic Settings
│       │   ├── deps.py                   # FastAPI dependency injection
│       │   └── logging.py                # structlog configuration
│       ├── db/
│       │   ├── base.py                   # DeclarativeBase + TimestampMixin
│       │   └── session.py                # Async engine + session factory
│       ├── models/                       # SQLAlchemy ORM models
│       │   ├── enums.py
│       │   ├── order.py
│       │   ├── signal.py
│       │   ├── scan_cycle.py
│       │   ├── scan_symbol_result.py
│       │   ├── credentials.py
│       │   ├── audit_log.py
│       │   ├── observed_position.py
│       │   ├── position_pnl_snapshot.py
│       │   ├── auto_mode_drift_symbol.py
│       │   ├── aqrr_trade_stat.py
│       │   └── settings.py
│       ├── routers/                      # FastAPI routers
│       │   ├── auto_mode.py
│       │   ├── credentials.py
│       │   ├── history.py
│       │   ├── orders.py
│       │   ├── scan.py
│       │   ├── settings.py
│       │   ├── signals.py
│       │   └── status.py
│       ├── schemas/                      # Pydantic I/O models
│       ├── services/                     # All domain logic
│       │   ├── auto_mode.py             (~98 KB — core orchestration)
│       │   ├── order_manager.py         (~267 KB — trade execution)
│       │   ├── scanner.py               (~62 KB — market scan)
│       │   ├── binance_gateway.py        # API wrapper
│       │   ├── lifecycle_monitor.py      # Order sync loop
│       │   ├── market_health.py          # Real-time book quality
│       │   ├── user_data_stream.py       # WebSocket user events
│       │   ├── position_observer.py      # Position reconciliation
│       │   ├── scheduler.py              # APScheduler wrapper
│       │   ├── settings.py               # Dynamic settings store
│       │   ├── notifier.py               # Desktop notifications
│       │   ├── audit.py                  # Audit log helper
│       │   ├── order_sizing.py
│       │   ├── partial_tp.py
│       │   ├── runtime_cache.py
│       │   ├── ws_manager.py
│       │   └── strategy/
│       │       ├── aqrr.py              # Core AQRR evaluation
│       │       ├── config.py            # StrategyConfig dataclass
│       │       ├── indicators.py        # Technical indicators
│       │       ├── statistics.py        # Bucket hit-rate tracking
│       │       └── types.py             # Shared types / helpers
└── frontend/
    ├── package.json
    ├── vite.config.ts
    └── src/
        ├── App.tsx                       # React router root
        ├── store/appStore.ts             # Zustand global state
        ├── pages/
        │   ├── DashboardPage.tsx
        │   ├── AutoModePage.tsx
        │   ├── SignalsPage.tsx
        │   ├── OrdersPage.tsx
        │   ├── HistoryPage.tsx
        │   ├── SettingsPage.tsx
        │   └── CredentialsPage.tsx
        └── components/
            ├── layout/
            ├── orders/
            ├── signals/
            ├── settings/
            └── ui/
```

---

## 3. Technical Stack

### Backend

| Technology | Version | Role |
|---|---|---|
| Python | 3.14 | Runtime |
| FastAPI | 0.115.12 | HTTP API framework |
| Uvicorn (standard) | 0.34.0 | ASGI server |
| SQLAlchemy (async) | 2.0.48 | ORM |
| asyncpg | 0.30.0 | PostgreSQL async driver (runtime) |
| psycopg2-binary | 2.9.11 | PostgreSQL sync driver (Alembic migrations only) |
| Alembic | 1.14.1 | Database migrations |
| APScheduler | 3.11.0 | Cron-based scan scheduling |
| httpx | 0.28.1 | Binance REST API HTTP client |
| websockets | (transitive) | Binance user-data WebSocket |
| pydantic-settings | 2.8.1 | Typed environment configuration |
| structlog | 25.2.0 | Structured JSON logging |
| cryptography | 44.0.2 | RSA signing of Binance API requests |
| python-dotenv | 1.1.0 | `.env` file loading |
| plyer | 2.1.0 | Desktop notification alerts |
| greenlet | 3.3.2 | SQLAlchemy async threading |

### Frontend

| Technology | Version | Role |
|---|---|---|
| React | 19.0.0 | UI framework |
| TypeScript | 5.8.2 | Type safety |
| Vite | 6.2.3 | Build tool / dev server |
| React Router DOM | 7.4.0 | Client-side routing |
| Zustand | 5.0.3 | Global state management |
| Axios | 1.9.0 | HTTP client |
| Vitest | 3.0.9 | Unit tests |
| @testing-library/react | 16.3.0 | Component testing |

### Infrastructure

| Component | Technology |
|---|---|
| Database | PostgreSQL (local) |
| API authentication | RSA Ed25519 / HMAC-SHA256 (Binance) |
| Process management | Bash scripts (`start.sh`, `stop.sh`) |

---

## 4. System Architecture

### High-Level Component Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│  Frontend (Vite + React)                                             │
│  Port 3000                                                           │
│  DashboardPage │ AutoModePage │ SignalsPage │ OrdersPage │ Settings  │
└────────────────────────────┬─────────────────────────────────────────┘
                              │ REST / Axios (localhost)
┌────────────────────────────▼─────────────────────────────────────────┐
│  Backend (FastAPI + Uvicorn)                                         │
│  Port 8000                                                           │
│  /api/status │ /api/auto-mode │ /api/signals │ /api/orders          │
│  /api/credentials │ /api/settings │ /api/scan │ /api/history        │
│  /ws  (WebSocket — server push)                                      │
└──────────────────────────────────────────────────────────────────────┘
         │                       │                      │
         ▼                       ▼                      ▼
  ┌────────────┐        ┌────────────────┐    ┌─────────────────────┐
  │ PostgreSQL │        │ Binance REST   │    │ Binance WebSocket   │
  │ (local DB) │        │ USD-M Futures  │    │ User Data Stream    │
  └────────────┘        └────────────────┘    └─────────────────────┘
```

### Service Dependency Graph (Backend)

```
main.py (lifespan)
  ├── BinanceGateway          ← All Binance HTTP calls
  ├── WebSocketManager        ← Broadcasts to connected browser clients
  ├── NotifierService         ← Desktop alerts (plyer)
  ├── MarketHealthService     ← Async background loop, book quality
  ├── OrderManager            ← Trade execution & lifecycle
  │     └── BinanceGateway, WebSocketManager, NotifierService
  ├── ScannerService          ← Market scanner
  │     └── BinanceGateway, WebSocketManager, OrderManager,
  │          NotifierService, MarketHealthService
  ├── AutoModeService         ← Orchestrator (scan → signal → order)
  │     └── ScannerService, OrderManager, WebSocketManager,
  │          NotifierService, BinanceGateway
  ├── LifecycleMonitor        ← 60-second order sync loop
  │     └── OrderManager, PositionObserver, AutoModeService
  ├── UserDataStreamSupervisor ← Binance WS event pump
  │     └── BinanceGateway (listen-key mgmt), LifecycleMonitor
  ├── PositionObserver        ← Reconciles open positions from exchange
  └── SchedulerService        ← APScheduler (every 15m at :05)
        └── AutoModeService
```

---

## 5. Startup & Operational Workflow

### `start.sh` — Full Bootstrap Sequence

The `start.sh` script is the single entry point for running the entire stack. It performs the following steps in order:

1. **Prerequisite checks** — asserts `python3`, `npm`, `curl`, `lsof` are available.
2. **Load `.env`** — reads `DATABASE_URL`, `BACKEND_PORT` (default 8000), `FRONTEND_PORT` (default 3000).
3. **Lock acquisition** — uses a directory-based mutex (`$RUN_DIR/start.lock`) to prevent concurrent startups.
4. **Already-running detection** — checks PID files and live HTTP health; exits gracefully if already up.
5. **Port conflict check** — fails early if ports 8000 / 3000 are occupied by unmanaged processes.
6. **Backend virtualenv + deps** — creates `.venv` the first time; re-installs only when `requirements.txt` SHA256 hash changes.
7. **Frontend Node modules** — runs `npm install` only when `package.json` / `package-lock.json` hash changes.
8. **Preflight settings validation** — instantiates `get_settings()` inside the venv to catch config errors before daemonizing.
9. **Database connectivity** — opens a raw TCP socket to the DB host/port, then runs a `SELECT 1` via `asyncpg`.
10. **Alembic migrations** — runs `alembic upgrade head` against the live database.
11. **Backend daemonize** — starts `uvicorn app.main:app` with `nohup`, records PID in `.run/backend.pid`.
12. **Frontend daemonize** — starts `vite --host --port`, records PID in `.run/frontend.pid`.
13. **Health poll** — polls `GET /api/status` and the Vite root until both respond (60-second timeout each).
14. **Status file** — writes `.run/status.env` with all PIDs, ports, and log paths for `stop.sh` to read.
15. **Open browser** — calls `open $FRONTEND_URL` on macOS.

Logs are written to `logs/backend.log` and `logs/frontend.log`.

### `stop.sh` — Clean Shutdown

1. Reads `.run/status.env` (falls back to `.env` for ports).
2. Reads backend/frontend PIDs from `.run/*.pid`.
3. Falls back to `lsof -tiTCP:<port>` discovery if PID files are stale.
4. Verifies each PID's command line matches the expected pattern before sending `SIGTERM`.
5. Waits up to 15 seconds; escalates to `SIGKILL` if necessary.
6. Deletes `.run/backend.pid`, `.run/frontend.pid`, `.run/status.env`, and the lock dir.

---

## 6. Environment & Configuration

### `.env` Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | `postgresql+asyncpg://user:pass@host:port/db` |
| `BACKEND_HOST` | No (default `127.0.0.1`) | Uvicorn bind host |
| `BACKEND_PORT` | No (default `8000`) | Uvicorn bind port |
| `FRONTEND_HOST` | No (default = BACKEND_HOST) | Vite bind host |
| `FRONTEND_PORT` | No (default `3000`) | Vite dev server port |
| `CORS_ORIGINS` | Yes | Comma-separated allowed origins |
| `BINANCE_BASE_URL` | No | Override Binance REST base (useful for testnet) |
| `LIFECYCLE_POLL_SECONDS` | No (default `60`) | LifecycleMonitor poll interval |

### `Settings` (Pydantic)

Defined in `backend/app/core/config.py`, loaded once at startup with `@lru_cache`. Exposes:

- `database_url`
- `cors_origins: list[str]` (parsed from `CORS_ORIGINS`)
- `binance_base_url` (defaults to live USD-M Futures endpoint)
- `lifecycle_poll_seconds`

### Dynamic Strategy Settings

Strategy runtime parameters are stored in the PostgreSQL `settings` table (key-value store of `SettingsEntry` rows). They are resolved at each scan cycle by `get_settings_map(session)` and merged into a `StrategyConfig` dataclass by `resolve_strategy_config(settings_map)`. This allows operators to tune the strategy without restarting the backend.

Key configurable parameters include:

| key | Meaning |
|---|---|
| `auto_mode_enabled` | Master on/off switch |
| `auto_mode_paused` | Suspend scans without stopping |
| `risk_per_trade_fraction` | Fraction of available balance risked per trade |
| `max_portfolio_risk_fraction` | Maximum total open risk as fraction of balance |
| `max_book_spread_bps` | Hard spread filter (default 12 bps) |
| `min_24h_quote_volume_usdt` | Configured liquidity floor |
| `account_maker_fee_rate` | Override API-fetched maker fee |
| `account_taker_fee_rate` | Override API-fetched taker fee |
| `max_shared_entry_slots` | Maximum concurrent active orders (default 3) |

---

## 7. Database Layer

### Connection

`backend/app/db/session.py` creates:
- A single `create_async_engine` bound to `settings.database_url`, with `pool_pre_ping=True`.
- An `async_sessionmaker` used throughout the backend as `AsyncSessionLocal`.
- A `get_db()` async generator for FastAPI dependency injection.

### Migrations

Alembic (`alembic.ini` + `backend/alembic/`) manages all DDL changes. The script runner is invoked by `start.sh` on every startup via `alembic upgrade head`. For migrations, Alembic uses `psycopg2-binary` (synchronous driver), while the application runtime uses `asyncpg`.

### ORM Models

All models inherit from `Base` (SQLAlchemy `DeclarativeBase`). Timestamped models additionally inherit from `TimestampMixin`, which provides `created_at` and `updated_at` columns with server-side defaults.

| Model | Table | Purpose |
|---|---|---|
| `Order` | `orders` | Primary trade record — full lifecycle |
| `Signal` | `signals` | Strategy output awaiting approval / execution |
| `ScanCycle` | `scan_cycles` | One scanner run (metadata + counters) |
| `ScanSymbolResult` | `scan_symbol_results` | Per-symbol audit row for each scan |
| `ApiCredentials` | `api_credentials` | Single-row store for Binance key + RSA PEM |
| `SettingsEntry` | `settings` | Dynamic key-value configuration |
| `AuditLog` | `audit_log` | Structured event log for all actions |
| `ObservedPosition` | `observed_positions` | Live positions seen on exchange |
| `PositionPnlSnapshot` | `position_pnl_snapshots` | Time-series PnL per position |
| `AutoModeDriftSymbol` | `auto_mode_drift_symbols` | Symbols flagged by drift tracking |
| `AqrrTradeStat` | `aqrr_trade_stats` | Closed-trade win/loss bucket statistics |

---

## 8. Backend Services — Detailed Reference

### 8.1 BinanceGateway

**File:** `backend/app/services/binance_gateway.py`

The single-responsibility HTTP adapter for all Binance USD-M Futures REST calls. It wraps `httpx.AsyncClient` and signs requests using RSA (Ed25519) private keys stored in the database.

#### Key responsibilities

- **Request signing.** Attaches `timestamp`, `signature` (RSA or HMAC-SHA256 depending on credential mode), and `X-MBX-APIKEY` headers.
- **Server-time synchronisation.** At startup, fetches Binance server time and stores `server_time_offset_ms`. This offset is added to every signed timestamp to prevent clock-drift rejects.
- **Rate-limit awareness.** Reads `X-MBX-USED-WEIGHT-*` response headers.
- **Error classification.** Parses Binance JSON error codes. Special handling for:
  - Risk-engine errors (code `-2019` etc.) → raises `BinanceAPIError` with `is_risk_error=True`.
  - Timestamp-drift errors (code `-1021`) → triggers automatic time-sync and retry.
- **Caching.** Provides `AsyncTTLCache`-backed wrappers (`read_cached_exchange_info`, `read_cached_mark_prices`, `read_cached_leverage_brackets`) to avoid redundant fetches during a scan cycle.
- **Symbol filter parsing.** `parse_symbol_filters(exchange_info)` returns a `SymbolFilters` dataclass per symbol containing `tick_size`, `step_size`, `min_qty`, `min_notional`, `market_step_size`.

#### Core API methods

| Method | Endpoint | Notes |
|---|---|---|
| `ping()` | `GET /fapi/v1/ping` | Connectivity check |
| `exchange_info()` | `GET /fapi/v1/exchangeInfo` | Symbol rules & filters |
| `klines(symbol, interval, limit)` | `GET /fapi/v1/klines` | OHLCV candles |
| `klines_history(...)` | Multi-page fetch | For warm-start |
| `ticker_24hr()` | `GET /fapi/v1/ticker/24hr` | Quote volumes |
| `book_tickers()` | `GET /fapi/v1/ticker/bookTicker` | Best bid/ask |
| `mark_prices()` | `GET /fapi/v1/premiumIndex` | Mark price + funding |
| `mark_price(symbol)` | `GET /fapi/v1/premiumIndex?symbol=...` | Single symbol |
| `funding_rate_history(symbol)` | `GET /fapi/v1/fundingRate` | Cost estimation |
| `account_info(credentials)` | `GET /fapi/v2/account` (signed) | Balance + positions |
| `commission_rate(credentials, symbol)` | `GET /fapi/v1/commissionRate` (signed) | Live fee rates |
| `leverage_brackets(credentials)` | `GET /fapi/v1/leverageBracket` (signed) | Max leverage per tier |
| `set_leverage(credentials, symbol, lev)` | `POST /fapi/v1/leverage` (signed) | Pre-trade setup |
| `new_order(credentials, ...)` | `POST /fapi/v1/order` (signed) | Place any order type |
| `cancel_order(credentials, symbol, ...)` | `DELETE /fapi/v1/order` (signed) | Cancel by ID / clientId |
| `query_order(credentials, symbol, ...)` | `GET /fapi/v1/order` (signed) | Single order state |
| `query_open_orders(credentials, symbol)` | `GET /fapi/v1/openOrders` (signed) | All active orders |
| `positions(credentials)` | `GET /fapi/v2/positionRisk` (signed) | Active positions |
| `start_user_data_stream(credentials)` | `POST /fapi/v1/listenKey` (signed) | Get listenKey |
| `keepalive_user_data_stream(...)` | `PUT /fapi/v1/listenKey` (signed) | Renew every 25 min |
| `close_user_data_stream(...)` | `DELETE /fapi/v1/listenKey` (signed) | On shutdown |
| `user_data_stream_ws_url(listenKey)` | — | Constructs `wss://` URL |

---

### 8.2 ScannerService

**File:** `backend/app/services/scanner.py` (1 290 lines)

Executes one complete market scan cycle. Called by `AutoModeService` once per 15-minute candle close (or on demand).

#### Scan pipeline

```
run_scan(session, trigger_type=AUTO_MODE)
  │
  ├── Load settings + StrategyConfig
  ├── Fetch exchange_info, ticker_24hr, book_tickers, mark_prices
  ├── Fetch account snapshot + active orders (for risk budgets)
  ├── Compute SharedEntrySlotBudget  (remaining slots, deployable equity)
  ├── Compute per-trade risk cap (risk_per_trade_fraction × balance)
  ├── Build eligible symbol universe (perpetual USDT/USDC, status=TRADING)
  ├── Compute dynamic liquidity floor  (30th percentile of 24h quote volumes;
  │    fallback = $25M if no volumes available)
  ├── Order symbols by quote volume DESC (priority symbols first)
  ├── Fetch BTC 1h returns (for correlation baseline)
  │
  └── FOR EACH SYMBOL:
        ├── Check exchange filters exist
        ├── Get MarketHealthSnapshot (book stability, spread, spread_median)
        ├── Filter: order_book_unstable
        ├── Filter: spread_unavailable
        ├── Filter: spread_bps > max_book_spread_bps (12 bps)
        ├── Filter: spread_relative_ratio > 2.5× median (if ≥60 samples)
        ├── Filter: quote_volume < liquidity_floor
        ├── Fetch 15m + 1h + 4h candles
        ├── evaluate_symbol() → SetupCandidate or rejection
        ├── Apply per-symbol filters (execution tier, score threshold)
        ├── Rank candidates via calibrated hit-rate stats
        └── Store ScanSymbolResult + diagnostic log line
  │
  ├── rank_candidates(candidates)       # cross-sectional sort by rank_value
  ├── select_candidates(ranked, budget) # pick top N respecting slot budget
  ├── For each selected candidate:
  │     ├── Create Signal (status=QUALIFIED)
  │     └── Store ScanSymbolResult (outcome=SELECTED)
  ├── Update ScanCycle (status=COMPLETE, counters)
  └── Write diagnostic_scan.log line
```

#### Diagnostic log

Every symbol evaluation appends a structured JSON line to `logs/diagnostic_scan.log`, capturing the full decision context: spread, volume, market state, setup family, score breakdown, rank value, rejection reasons, and entry/stop/take-profit prices.

---

### 8.3 AutoModeService

**File:** `backend/app/services/auto_mode.py` (~98 KB)

The central orchestrator. Maintains an asyncio-based state machine that drives the full automated trading loop.

#### State machine

```
IDLE  ←──────────────────────────────────────────────────────┐
  │                                                           │
  │ queue_cycle() / scheduler fires                           │
  ▼                                                           │
SCANNING  (runs ScannerService.run_scan)                      │
  │                                                           │
  │ scan complete                                             │
  ▼                                                           │
SIGNAL_PROCESSING  (consume qualified signals → create orders) │
  │                                                           │
  │ all signals processed                                     │
  ▼                                                           │
MONITORING  (LifecycleMonitor + manage_live_positions)        │
  │                                                           │
  │ next scan due or queue_cycle() called ─────────────────────┘
```

#### Key concepts

**Kill switch** — If the strategy's internal safety conditions are not met (e.g. consecutive losses, balance drawdown beyond threshold), `AutoModeService` can engage a kill switch that halts all new order placement. Active orders are cancelled, and the state is broadcast to the frontend.

**Drift tracking** — An active entry order's mark price is compared to its entry price every lifecycle cycle. If the distance exceeds a configurable threshold (`auto_mode_max_entry_distance_pct`), the order is cancelled and the symbol is recorded in `auto_mode_drift_symbols`. A re-qualification check runs on the next scan.

**Regime flip** — If the strategy detects that a symbol's market state has changed since the order was placed (e.g. Bull → Unstable), it cancels the pending entry order with reason `regime_flipped`.

**manage_live_positions** — Called every `LifecycleMonitor` tick. Applies ongoing signal validation to IN_POSITION orders: spread re-check, volatility shock detection, structure break re-evaluation.

**Concurrency control** — Uses `asyncio.Lock` to prevent two simultaneous scan cycles. A second call to `queue_cycle()` while a scan is running is queued for immediate execution upon completion.

**WebSocket broadcast** — After every state change, a structured payload is pushed to all connected frontend clients via `WebSocketManager`.

---

### 8.4 OrderManager

**File:** `backend/app/services/order_manager.py` (~267 KB)

The largest single file in the project. Responsible for the full mechanical lifecycle of every trade: from pre-flight checks and order submission to fill detection, protective order management, and closed-trade recording.

#### Pre-flight checks

Before placing any order, the manager verifies:
- Credentials available
- Account balance is sufficient after 10% reserve
- Symbol passes min-notional with the proposed position size
- Leverage bracket allows the requested leverage
- Liquidation price is at least 3% beyond stop-loss (×1.25 buffer)
- Spread is within acceptable range at submission time
- Slot budget is not exhausted

#### Order placement flow

```
approve_signal(session, signal_id, approved_by="AUTO_MODE")
  ├── Pre-flight validation
  ├── set_leverage() on exchange
  ├── Place entry order (LIMIT, STOP, or GTD variants)
  │     ├── Try GTD (Good-Till-Date) with expiry from signal
  │     └── Fallback to GTC if exchange rejects GTD
  ├── Create Order row (status=SUBMITTING → ORDER_PLACED)
  └── Return order
```

#### Protection orders

Once an order transitions to `IN_POSITION` (i.e. the entry fill is confirmed):
- Standard mode: one TAKE_PROFIT_MARKET + one STOP_MARKET
- Partial TP mode: two take-profit orders at different price levels + one stop

Client order IDs follow the pattern `fbot.<order_id>.<role>` where role ∈ `{entry, tp, tp1, tp2, sl}`.

#### Slot budget

`build_shared_entry_slot_budget()` computes:
- `slot_cap` = `max_shared_entry_slots` (default 3)
- `remaining_entry_slots` = slot_cap minus current active order count
- `per_slot_budget` = deployable equity ÷ remaining slots

#### User stream integration

`OrderManager` tracks liveness of the Binance user data stream. If the stream has been silent for more than `3 × lifecycle_poll_seconds` (minimum 120 s) for two consecutive checks while active orders exist, it marks the stream as stale and falls back to pure polling.

#### Cancel reasons (normalised)

| Canonical reason | Triggers |
|---|---|
| `expired` | Order validity window elapsed |
| `regime_flipped` | Market state changed |
| `setup_state_changed` | Price structure invalidated |
| `spread_filter_failed` | Spread deteriorated after entry |
| `volatility_shock` | ATR spike beyond threshold |
| `structure_invalidated` | Key level broken |
| `correlation_conflict` | New position conflicts with existing |
| `viability_lost` | Score dropped below threshold; kill-switch |
| `manual_cancel` | Operator manual cancel |

---

### 8.5 LifecycleMonitor

**File:** `backend/app/services/lifecycle_monitor.py`

A perpetual `asyncio.Task` that runs an order-reconciliation loop.

#### Loop logic

1. Wake up every `poll_seconds` (default 60) **or immediately** when `notify_exchange_event()` is called by the `UserDataStreamSupervisor`.
2. Drain pending exchange events from the `OrderManager`.
3. Call `order_manager.reconcile_managed_orders(session)` — syncs order state with in-memory cache.
4. If an `ACCOUNT_UPDATE` event is pending, call `position_observer.sync_positions(session)` first (prioritised).
5. Query all `SUBMITTING`, `ORDER_PLACED`, `IN_POSITION` orders.
6. For each order:
   - Call `order_manager.sync_order(session, order)` — fetches & reconciles state from Binance.
   - If `ORDER_PLACED` and expired, call `cancel_order(session, order_id, reason="expired")`.
   - If `IN_POSITION`, call `cancel_sibling_pending_orders(session, order)` — kills stale entry orders for the same symbol.
7. Sync positions if not already done (step 4).
8. Call `auto_mode_service.manage_live_positions(session)`.
9. Commit session.

---

### 8.6 UserDataStreamSupervisor

**File:** `backend/app/services/user_data_stream.py`

Manages the Binance WebSocket user data stream lifecycle.

#### Flow

1. Polls for credentials every 5 seconds until found.
2. Calls `gateway.start_user_data_stream(credentials)` → gets `listenKey`.
3. Opens `wss://fstream.binance.com/ws/<listenKey>` via `websockets.connect`.
4. Sends keepalive `PUT /fapi/v1/listenKey` every 25 minutes.
5. On `ORDER_TRADE_UPDATE` or `ACCOUNT_UPDATE` events, calls `lifecycle_monitor.notify_exchange_event(payload)`.
6. If `LISTENKEYEXPIRED` received, raises `RuntimeError` → reconnects after 5 s.
7. On disconnect or error, closes the `listenKey` and reconnects with 5 s back-off.
8. Notifies `OrderManager.set_user_stream_primary_path_availability()` on connect/disconnect.

---

### 8.7 SchedulerService

**File:** `backend/app/services/scheduler.py`

Thin wrapper around `apscheduler.schedulers.asyncio.AsyncIOScheduler`.

- On startup (`start()`): registers the `auto-mode-scan` cron job (`*/15 * * * * :05`) if `auto_mode_enabled=true` and `auto_mode_paused=false`.
- On `patch_settings` API calls that change Auto Mode state: calls `scheduler.reload()` to add or remove the job.
- On each trigger: calls `auto_mode_service.run_cycle(reason="15m_close")`.

---

### 8.8 MarketHealthService

**File:** `backend/app/services/market_health.py`

A background service that continuously monitors order book quality for every symbol in the universe.

#### Data collected (per symbol, updated every 3 seconds)

- Current best bid/ask (from `book_tickers` batch call)
- Current mark price (from `mark_prices` batch call)
- Spread in basis points
- Rolling 24-hour spread history → **median spread** (recomputed every 30 seconds when book is stable)
- 5-minute book stability history (mid-price, spread, touch notional)

#### Book stability assessment

A symbol fails the `book_stable` check if **any** of:

| Condition | Reason tag |
|---|---|
| Touch notional (min(bid×bidQty, ask×askQty)) < $15 | `touch_liquidity_thin` |
| Mid-price velocity > `max(8 bps, 3.5× spread)` with ≥1 direction reversal in last 30 s | `erratic_quote_movement` |
| Spread whipsaw: max/median > 2.5 with direction changes | `spread_whipsaw` |
| Mark-price gap > `max(12 bps, 4× spread)` | `book_mark_divergence` |

This service is injected into `ScannerService` so each symbol's health is evaluated with live intra-cycle data, not stale snapshots.

---

### 8.9 PositionObserver

**File:** `backend/app/services/position_observer.py`

Periodically reconciles the `observed_positions` table against live exchange positions retrieved via `gateway.positions(credentials)`.

- Creates or updates `ObservedPosition` rows.
- Marks positions as closed if no longer present on the exchange.
- Exposes `position_rows(session)` and `portfolio_summary(session)` for the REST API.
- Stores periodic PnL snapshots in `position_pnl_snapshots`.

---

### 8.10 SettingsService

**File:** `backend/app/services/settings.py`

Manages the dynamic key-value configuration store.

- `get_settings_map(session) → dict[str, str]`: loads all `SettingsEntry` rows plus built-in defaults.
- `patch_settings(session, updates: dict[str, str]) → dict[str, str]`: validates keys against a whitelist, applies range constraints, upserts rows, returns merged map.
- `resolve_strategy_config(settings_map) → StrategyConfig`: converts the string map to a typed, validated `StrategyConfig` dataclass used throughout scan and order logic.

Default factory values for all strategy parameters are hard-coded in `StrategyConfig` and listed in `backend/app/services/strategy/config.py`.

---

### 8.11 NotifierService

**File:** `backend/app/services/notifier.py`

Desktop notification wrapper using the `plyer` library. Called by `AutoModeService` and `OrderManager` on significant events (new signals found, orders placed, positions closed). Fails silently if the notification subsystem is unavailable.

---

## 9. AQRR Strategy Engine

The complete authoritative specification lives in [`AQRR_Binance_USDM_Strategy_Spec.md`](./AQRR_Binance_USDM_Strategy_Spec.md). This section summarises the implementation as it exists in code.

### 9.1 Philosophy

AQRR is designed around five non-negotiable principles:

1. **No-trade is a valid output** — the system is rewarded for selectivity, not activity.
2. **High quality = realistic, not perfect** — a setup must justify risk and rank above competitors.
3. **Regime-first** — market state is classified before any setup is evaluated.
4. **Execution reality is part of the strategy** — spread, slippage, fees, and leverage are first-class inputs.
5. **Ranking > signal count** — cross-sectional selection promotes the strongest 0–3 candidates.

### 9.2 Market State Classification

`classify_market_state(candles_15m, candles_1h, candles_4h)` in `aqrr.py` returns one of:

| State | Conditions |
|---|---|
| `bull_trend` | 1h EMA50 > EMA200; 4h EMA50 ≥ EMA200; ADX(14) ≥ 22; positive EMA50 slope; no vol shock |
| `bear_trend` | 1h EMA50 < EMA200; 4h EMA50 ≤ EMA200; ADX(14) ≥ 22; negative EMA50 slope; no vol shock |
| `balanced_range` | ADX(14) ≤ 18; Bollinger bandwidth below expansion threshold; price repeatedly mean-crossing EMA50 |
| `unstable` | Conflicting signals, transitional, extreme ATR percentile, spread abnormal, or pump/dump profile |

Indicators used: EMA(20), EMA(50), EMA(200), ADX(14), ATR(14), Bollinger Bands, RSI(14), swing structure, volatility percentile.

### 9.3 Setup Families

Only one family is allowed per symbol per scan.

#### Trend Continuation (Bull or Bear regime)

Two variants:

1. **Breakout-Retest Continuation**
   - Breakout above 15m resistance / 20-bar high (long) or below 20-bar low (short)
   - Breakout quality filters: candle body ≥ 55% of range, range ≤ 1.8× ATR(15m), volume participation
   - Entry: passive limit at breakout level (primary) or stop entry above confirmation candle (secondary)
   - Stop: below retest swing low or below breakout level minus buffer = `max(0.20×ATR15m, 3×tick_size, 2×spread)`
   - Expiry: 3 closed 15m candles or immediate regime deterioration

2. **Pullback Continuation**
   - Price retraces into EMA20/EMA50 support zone in Bull regime (or resistance in Bear)
   - Rejection signal required: wick, engulf, higher-low formation, or momentum loss
   - Entry: limit within pullback zone
   - Expiry: 4 closed 15m candles or support break

#### Range Reversion (Balanced Range regime only)

Two variants:

1. **Long from lower range support** — RSI(14) stretched or recovering, slowing downside impulse, passive limit near support
2. **Short from upper range resistance** — mirror logic

Both require the 3R take-profit target to fit within the range before opposing structure.

### 9.4 Net 3R Feasibility Gate

Every candidate must satisfy:

```
net_R_multiple = (gross_reward - cost) / (R + cost) ≥ 3.0
```

Where:
- `R = |entry_price - stop_price|`
- `gross_reward = |target_price - entry_price|`
- `cost = entry_fee + exit_fee + expected_slippage + funding_cost_if_crossing_funding`

Fee cost model:
- Maker/taker rates fetched from `GET /fapi/v1/commissionRate` per symbol (or overridden via settings)
- Slippage estimated from current spread
- Funding cost included if next funding time is within expected hold period and |funding_rate| ≥ 0.04%

Additionally, a **structural barrier rule** rejects setups where a major opposing structure sits between entry and the required TP target.

### 9.5 Quality Scoring Engine

Final score out of 100 (weighted sum of normalised components):

| Component | Weight | What it measures |
|---|---|---|
| Structure Quality | 25 | Level clarity, invalidation quality, absence of messy overlap |
| Regime Alignment | 20 | Setup type vs market state; 1h/4h agreement |
| Confirmation Quality | 15 | Breakout participation, rejection wick, momentum decisiveness |
| Liquidity & Execution Quality | 15 | Spread, slippage estimate, book stability |
| Volatility Quality | 10 | ATR regime: healthy not dead, controlled not chaotic |
| Reward Headroom Quality | 10 | Distance to 3R target; opposing structure clearance |
| Funding & Carry Quality | 5 | Expected funding cost; adverse funding penalty |

Score thresholds:
- < 70 → Rejected
- 70–79 → Candidate (Tier A symbols); Tier B symbols need ≥ 78
- 80–89 → Strong candidate
- ≥ 90 → Exceptional

### 9.6 Candidate Ranking & Selection

`rank_candidates(candidates)` sorts by `rank_value` descending.

`select_candidates(ranked, slot_budget)` picks the top N, limited by:
- Available entry slots (≤ 3 simultaneous)
- Active symbols already traded (no duplicate symbol entries)
- Correlation conflict check (prevents three highly correlated altcoin longs, for example)

### 9.7 Historical Bucket Calibration

`build_candidate_stats_bucket()` constructs a composite key:

```
bucket_key = "<setup_family>|<direction>|<market_state>|<score_band>|<volatility_band>|<execution_tier>"
```

Example: `trend_continuation_breakout|LONG|bull_trend|80_89|normal|tier_a`

`calibrated_rank_value()` computes:

```
rank_value = 0.70 × final_score + 0.30 × hit_rate_score
```

Where `hit_rate_score = (win_count / closed_trade_count) × 100`, used only if `closed_trade_count ≥ 20`.

If fewer than 20 closed trades exist in a bucket, `rank_value = final_score` (no calibration).

`record_closed_trade_stat()` is called by `OrderManager` when an order closes, incrementing the relevant bucket's counters in `aqrr_trade_stats`.

---

## 10. API Layer — REST Endpoints

All routes are mounted under `/api` with a `/ws` WebSocket endpoint.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/status` | Health check (DB, Binance connectivity, time offset) |
| `GET` | `/api/account/balance` | USDT wallet/available/usable balance |
| `GET` | `/api/account/positions` | Live open positions |
| `GET` | `/api/account/portfolio-summary` | Aggregated PnL + margin summary |
| `GET` | `/api/credentials` | Masked API key status |
| `POST` | `/api/credentials` | Save new credentials |
| `DELETE` | `/api/credentials` | Remove credentials |
| `GET` | `/api/credentials/test` | Test connectivity, returns live balance |
| `GET` | `/api/auto-mode` | Auto Mode status (enabled, paused, next run, kill switch) |
| `PATCH` | `/api/auto-mode` | Toggle enabled / paused |
| `GET` | `/api/signals` | List signals (filterable by status, direction, cycle) |
| `GET` | `/api/signals/{id}` | Single signal detail |
| `GET` | `/api/signals/recommendations` | Top 3 qualified signals from last scan, with live readiness |
| `GET` | `/api/orders` | All Auto Mode orders (filterable by status, symbol) |
| `GET` | `/api/orders/active` | Active orders only |
| `GET` | `/api/orders/{id}` | Single order detail |
| `GET` | `/api/scan` | Last scan cycle + current progress |
| `POST` | `/api/scan` | Trigger manual scan (if allowed) |
| `GET` | `/api/settings` | All current settings |
| `PATCH` | `/api/settings` | Update one or more settings |
| `GET` | `/api/history` | Closed orders + PnL history |
| `WS` | `/ws` | Real-time server push (Auto Mode state, scan progress, orders) |

---

## 11. Data Models — SQLAlchemy

### Order

Primary trade record. Status enum progression:

```
PENDING_APPROVAL (manual mode only)
  → SUBMITTING
  → ORDER_PLACED
  → IN_POSITION
  → CLOSED_WIN | CLOSED_LOSS | CLOSED_BREAKEVEN | CLOSED_UNKNOWN | CANCELLED
```

Key fields:

| Field | Type | Notes |
|---|---|---|
| `symbol` | `String(50)` | e.g. `BTCUSDT` |
| `direction` | `Enum(SignalDirection)` | `LONG` / `SHORT` |
| `entry_price` | `Numeric(20,8)` | |
| `stop_loss` | `Numeric(20,8)` | |
| `take_profit` | `Numeric(20,8)` | |
| `status` | `Enum(OrderStatus)` | Lifecycle state |
| `approved_by` | `String(50)` | `AUTO_MODE` |
| `expires_at` | `DateTime(tz)` | Pending entry expiry |
| `risk_usdt_at_stop` | `Numeric(20,8)` | Dollar risk |
| `signal_id` | FK → `signals` | Source signal |
| `strategy_context` | `JSON` | Rich metadata (entry fill qty, entry expiry, protection qty) |
| `score_breakdown` | `JSON` | Component scores |
| `entry_order_id` | `String` | Binance order ID |
| `stop_order_id` | `String` | |
| `tp_order_id` | `String` | |
| `tp1_order_id`, `tp2_order_id` | `String` | Partial TP |
| `closed_pnl` | `Numeric(20,8)` | On close |
| `fill_price` | `Numeric(20,8)` | Actual entry fill |

### Signal

Strategy output from a scan cycle.

| Field | Type | Notes |
|---|---|---|
| `status` | `Enum(SignalStatus)` | `QUALIFIED` → `APPROVED` / `EXPIRED` / `REJECTED` |
| `scan_cycle_id` | FK → `scan_cycles` | |
| `symbol` | `String(50)` | |
| `direction` | `Enum(SignalDirection)` | |
| `final_score` | `Integer` | 0–100 |
| `rank_value` | `Numeric(10,4)` | Calibrated rank |
| `net_r_multiple` | `Numeric(10,4)` | Expected net reward |
| `setup_family` | `String(100)` | e.g. `trend_continuation_breakout` |
| `market_state` | `String(50)` | e.g. `bull_trend` |
| `entry_style` | `String(50)` | `limit` / `stop` |
| `extra_context` | `JSON` | Full diagnostic context |
| `strategy_context` | `JSON` | Expiry, buffer values |

### ScanCycle

One completed scanner run.

| Field | Notes |
|---|---|
| `trigger_type` | `AUTO_MODE` (only supported value) |
| `status` | `RUNNING` → `COMPLETE` / `FAILED` |
| `symbols_scanned` | Counter |
| `candidates_found` | Symbols that passed pre-filtering |
| `signals_qualified` | Signals created (score ≥ threshold, 3R passed) |
| `progress_pct` | 0.0–100.0, updated during scan |

### AuditLog

Append-only structured event log. Key event types:

`SCAN_STARTED`, `SCAN_COMPLETE`, `SIGNAL_QUALIFIED`, `ORDER_SUBMITTED`, `ORDER_PLACED`, `POSITION_OPENED`, `POSITION_CLOSED`, `ORDER_CANCELLED`, `CREDENTIALS_SAVED`, `CREDENTIALS_TESTED`, `AUTO_MODE_UPDATED`, `KILL_SWITCH_TRIGGERED`

---

## 12. Frontend Application

### Technology

React 19 SPA served by Vite on port 3000. State managed by a single Zustand store (`appStore.ts`). API calls via Axios (all to `http://localhost:8000/api`). Real-time updates via a WebSocket connection to `/ws`.

### Pages

| Page | Route | Purpose |
|---|---|---|
| `DashboardPage` | `/` | System health, balance, active positions, recent activity |
| `AutoModePage` | `/auto-mode` | Auto Mode controls (enable/pause/stop), kill switch status, scan progress, next run time |
| `SignalsPage` | `/signals` | Qualified signals from the latest scan; live readiness indicators |
| `OrdersPage` | `/orders` | Active and recent orders with status, P&L |
| `HistoryPage` | `/history` | Closed trade history, P&L summary |
| `SettingsPage` | `/settings` | All strategy and runtime settings |
| `CredentialsPage` | `/api-credentials` | Binance API key management + connection test |

### State Management

`appStore.ts` (Zustand) holds global state:
- System status (health, balance)
- Auto Mode status
- Active orders
- Latest signals/recommendations
- WebSocket connection state

The store follows a fetch-on-mount + WebSocket-update pattern: polling for initial state, then applying real-time diffs from the WebSocket broadcasts.

---

## 13. Auto Mode — Full Cycle Walkthrough

This section traces an end-to-end automated scan and trade placement.

```
00:00  SchedulerService fires: */15 :05 UTC
        └→ auto_mode_service.run_cycle(reason="15m_close")

00:01  AutoModeService acquires _cycle_lock
        └→ ScannerService.run_scan(trigger_type=AUTO_MODE)

00:01  ScanCycle created (status=RUNNING)
       fetch: exchange_info, ticker_24hr, book_tickers, mark_prices
       fetch: account snapshot, active orders
       compute: liquidity_floor (30th percentile = e.g. $87M)
       compute: slot_budget (3 slots; 1 active → 2 remaining)
       compute: target_risk_usdt per trade

00:02  Symbol scan loop (e.g. 287 perpetual symbols):
         BTCUSDT  → evaluated → bull_trend + pullback_continuation
                   → score=84, net_R=3.7  → Candidate
         ETHUSDT  → evaluated → balanced_range + range_long
                   → score=71, net_R=3.2  → Candidate
         SOLUSDT  → filtered → spread_above_threshold (14.2 bps)
         BNBUSDT  → filtered → order_book_unstable (touch_liquidity_thin)
         ... (283 more)

00:03  rank_candidates([BTCUSDT, ETHUSDT])
       BTCUSDT rank=84.0 (no stats yet), ETHUSDT rank=71.0
       select_candidates → [BTCUSDT] (2 slots but one was drift-cancelled)

00:03  BTCUSDT signal created:
         Signal(status=QUALIFIED, symbol="BTCUSDT", direction=LONG,
                final_score=84, rank_value=84.0, net_r_multiple=3.7,
                entry_price=67420.00, stop_loss=67080.00, take_profit=68440.00)

       ScanCycle updated: status=COMPLETE, signals_qualified=1

00:03  AutoModeService._process_qualified_signals()
        └→ order_manager.approve_signal(session, signal_id=42, approved_by="AUTO_MODE")

00:03  Pre-flight:
         balance=42.80 USDT, reserve=4.28 USDT, usable=38.52 USDT
         risk_per_trade = 1% × 42.80 = 0.428 USDT
         position_size = risk / stop_distance_pct = ...
         min_notional check: OK
         leverage bracket: 20× OK at this notional
         liquidation buffer check: OK (liq at least 3% beyond stop)
         spread at submission: 1.8 bps < 12 bps: OK

00:03  gateway.set_leverage("BTCUSDT", 20)
       gateway.new_order(type=LIMIT, side=BUY, price=67420.00,
                          timeInForce=GTD, goodTillDate=<15min+3candles>)
       → exchange returns orderId="8291045"

       Order created:
         Order(status=ORDER_PLACED, entry_order_id="8291045",
               symbol="BTCUSDT", direction=LONG,
               entry_price=67420, stop_loss=67080, take_profit=68440)

       AuditLog: ORDER_SUBMITTED
       WebSocket broadcast: order_placed event

00:15  LifecycleMonitor wakes: sync_order(BTCUSDT order)
         → gateway.query_order → status=FILLED at 67418.40
         → Order transitions: ORDER_PLACED → IN_POSITION
         → Protection orders placed:
             TAKE_PROFIT_MARKET at 68440 (clientOrderId fbot.7.tp)
             STOP_MARKET at 67080 (clientOrderId fbot.7.sl)
         AuditLog: POSITION_OPENED

01:20  UserDataStreamSupervisor receives ORDER_TRADE_UPDATE:
         TAKE_PROFIT_MARKET FILLED at 68442.10, realized_pnl=+1.02 USDT
         → LifecycleMonitor wakes immediately
         → sync_order: IN_POSITION → CLOSED_WIN
         → stop order cancelled
         → closed_pnl=+1.02 stored
         → AqrrTradeStat bucket win_count incremented
         AuditLog: POSITION_CLOSED (CLOSED_WIN)
         WebSocket broadcast: trade_closed event
         Desktop notification: "BTCUSDT +$1.02 🎯"
```

---

## 14. Order Lifecycle

```
Signal (QUALIFIED)
  │
  ▼ approve_signal()
Order (SUBMITTING)
  │
  ├─── Exchange error → Order (CANCELLED), AuditLog
  │
  ▼ Binance confirmed
Order (ORDER_PLACED)
  │
  ├─── expires_at reached → cancel_order() → Order (CANCELLED)
  ├─── drift > threshold → cancel_order(reason="setup_state_changed") → Order (CANCELLED)
  ├─── regime flip → cancel_order(reason="regime_flipped") → Order (CANCELLED)
  ├─── volatility shock → cancel_order(reason="volatility_shock") → Order (CANCELLED)
  │
  ▼ FILLED on exchange
Order (IN_POSITION)
  │ protection orders placed
  │
  ├─── TP filled → Order (CLOSED_WIN)
  ├─── SL filled → Order (CLOSED_LOSS)
  ├─── Partial fill + SL → Order (CLOSED_LOSS or CLOSED_BREAKEVEN)
  └─── Manual cancel + no fill → Order (CANCELLED)
```

---

## 15. Security Model

### Binance API Credentials

Credentials are stored in the `api_credentials` table as plaintext PEM strings. The system supports two signing modes:

1. **RSA Ed25519** — private key PEM is stored in `private_key_pem`; public key in `public_key_pem`. Signing uses `cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey`.
2. **HMAC-SHA256** — `api_secret` field (legacy mode).

The API key itself is stored in `api_key`. It is masked in all API responses (`first4...last4`).

### Access Control

There is **no user authentication** on the backend API. The system is designed for local-only deployment on `127.0.0.1`. CORS is restricted to origins listed in `CORS_ORIGINS` (e.g. `http://localhost:3000`).

### Log Redaction

Structlog is configured to redact known sensitive fields (API keys, secrets) from log output. The `BinanceGateway` does not log request headers.

### Secret Location Antipatterns to Avoid

- Do **not** commit `.env` (it is in `.gitignore`).
- Credentials in the DB are not encrypted at rest — additional disk-level encryption is the operator's responsibility for any non-local deployment.

---

## 16. Logging & Auditability

### Structured Logging (structlog)

All backend services use `get_logger(__name__)` (which returns a structlog `BoundLogger`). Log events are structured key-value pairs with a `logger` key set to the module path.

Log destinations: stdout → `logs/backend.log` (captured by `nohup`).

### Audit Log

`record_audit(session, event_type, message, details)` writes to `audit_log`. Every significant business event is recorded:
- System lifecycle (startup, shutdown, credential changes)
- Scan events (started, complete, failed)
- Trading events (signal qualified, order submitted, position opened/closed, order cancelled)
- Auto Mode state changes

All audit rows include `timestamp`, `event_type`, `level` (`INFO`/`WARNING`/`ERROR`), `message`, and a `details` JSON dict. Optional foreign keys: `scan_cycle_id`, `signal_id`, `order_id`, `symbol`.

### Diagnostic Scan Log

`logs/diagnostic_scan.log` — one NDJSON line per symbol evaluated per cycle. Contains the full context: spread, volume, market state, score breakdown, rejection reasons, candidate prices. Used for post-hoc strategy analysis.

---

## 17. Testing

### Backend Tests

Framework: `pytest` + `pytest-asyncio`. Config in `pytest.ini`.

Test files are co-located or in `tests/` directories alongside the service modules. Key test areas:
- `AutoModePage.test.tsx` — frontend component tests
- `DashboardPage.test.tsx`
- `SignalsPage.test.tsx`
- `appStore.test.ts` — Zustand store tests
- Backend service unit tests (pytest)

### Frontend Tests

Framework: Vitest + `@testing-library/react` + jsdom.

Run with `npm test` in `frontend/`.

### Running Backend Tests

```bash
cd BINANCE-CRYPTO-BOT
backend/.venv/bin/python -m pytest
```

---

## 18. Operational Runbook

### Start the Full Stack

```bash
cd BINANCE-CRYPTO-BOT
./start.sh
```

Prerequisites: `python3`, `npm`, `curl`, `lsof`, PostgreSQL running, `.env` configured.

### Stop the Full Stack

```bash
./stop.sh
```

### Monitor Logs

```bash
tail -f logs/backend.log
tail -f logs/frontend.log
tail -f logs/diagnostic_scan.log   # per-symbol scan detail
```

### Enable Auto Mode (via UI)

Navigate to `http://localhost:3000/auto-mode` → Enable.

Or via API:
```bash
curl -X PATCH http://localhost:8000/api/auto-mode \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'
```

### Save API Credentials (via UI)

Navigate to `http://localhost:3000/api-credentials`.

Or via API:
```bash
curl -X POST http://localhost:8000/api/credentials \
  -H 'Content-Type: application/json' \
  -d '{
    "api_key": "...",
    "public_key_pem": "-----BEGIN PUBLIC KEY-----\n...",
    "private_key_pem": "-----BEGIN PRIVATE KEY-----\n..."
  }'
```

### Test Connection

```bash
curl http://localhost:8000/api/credentials/test
```

### Trigger a Manual Scan

```bash
curl -X POST http://localhost:8000/api/scan
```

### Change Strategy Settings

Example: lower risk per trade to 0.5%:
```bash
curl -X PATCH http://localhost:8000/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"risk_per_trade_fraction": "0.005"}'
```

### Database Migrations (manual)

```bash
cd backend
../.venv/bin/python -m alembic upgrade head
```

### Check System Health

```bash
curl http://localhost:8000/api/status
```

Returns:
```json
{
  "backend_ok": true,
  "db_ok": true,
  "binance_reachable": true,
  "server_time_offset_ms": -12
}
```

---

## 19. Known Constraints & Risks

### Hard Constraints

| Constraint | Detail |
|---|---|
| **Live USD-M Futures only** | `BINANCE_BASE_URL` defaults to `https://fapi.binance.com`. No paper-trading mode is implemented in the exchange adapter. |
| **Single-user, local-only** | No auth layer. Exposing the backend on a public network is a security risk. |
| **PostgreSQL required** | asyncpg does not support SQLite. |
| **Auto Mode off by default** | `auto_mode_enabled=false` at first run. Must be explicitly enabled. |
| **Max 3 simultaneous trades** | Hard-coded in `OrderManager.MAX_SHARED_ENTRY_ORDERS`. |
| **10% balance reserve** | Always held back; not deployable. Configurable only via source code. |
| **Minimum 20 bucket samples** | Calibrated hit-rate scoring requires ≥20 closed trades per bucket to activate. New accounts rank by raw score only. |

### Operational Risks

| Risk | Mitigation |
|---|---|
| Clock drift → Binance timestamp rejects | `server_time_offset_ms` sync at startup; auto-retry on -1021 errors |
| User stream drops → missed fills | LifecycleMonitor polls every 60 s as fallback; degraded-mode banner in UI |
| Scan takes longer than 15 minutes | `_is_running` flag blocks overlapping scans; next scheduled trigger skips |
| Liquidation before stop | `MIN_LIQUIDATION_GAP_PCT = 3%` enforced pre-trade; `LIQUIDATION_BUFFER_MULTIPLE = 1.25` |
| Partial fills leaving unprotected positions | `sync_order` detects partial fill states and reconciles protection orders |
| Stale exchange_info cache | `read_cached_exchange_info` has a TTL; full refresh on each scan cycle |
| Concurrent DB writes | All mutations go through `AsyncSession` with `await session.commit()` after each logical unit |
| GTD order type not supported by sub-accounts | Auto-detected and falls back to GTC on first `BinanceAPIError` with code -1100/-1116 |

### Design Gaps / Future Work

- **No testnet support** — adding testnet requires a `BINANCE_TESTNET_BASE_URL` env var and credential set.
- **Backtest / walk-forward** — `backend/scripts/aqrr_validation.py` is a scaffold stub only; no historical dataset handling is implemented.
- **No mobile / remote access** — the frontend only works on `localhost`.
- **Partial TP** — the `_partial_tp_requested()` method always returns `False` (stub). The database columns and split-quantity logic exist but the feature is disabled.
- **Correlation filter** — the spec describes a rolling correlation filter; the implementation uses lighter-weight thematic / beta clustering checks.

---

## 20. Validation Ladder

The project includes a formal validation progression documented in `docs/AQRR_VALIDATION_LADDER.md`:

| Stage | Command | Goal |
|---|---|---|
| Backtest | `python backend/scripts/aqrr_validation.py backtest --symbol BTCUSDT` | Confirm runner is wired; run on historical dataset |
| Walk-Forward | `python backend/scripts/aqrr_validation.py walk-forward --input data/walk_forward_config.json` | Sequential train/test slices preserving ranking and sizing |
| Paper Trading | `python backend/scripts/aqrr_validation.py paper --output reports/paper` | Full lifecycle without live capital |
| Micro-Live | `python backend/scripts/aqrr_validation.py micro-live` | + complete `AQRR_MICRO_LIVE_READINESS.md` checklist |

> **Note:** The validation script is currently a **scaffold stub** that prints JSON payloads. The actual backtest and walk-forward engines are not yet implemented.

---

*End of document. For strategy-level detail, refer to [AQRR_Binance_USDM_Strategy_Spec.md](./AQRR_Binance_USDM_Strategy_Spec.md). For validation requirements, refer to [docs/AQRR_MICRO_LIVE_READINESS.md](./docs/AQRR_MICRO_LIVE_READINESS.md).*
