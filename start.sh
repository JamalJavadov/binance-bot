#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
RUN_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/logs"
LOCK_DIR="$RUN_DIR/start.lock"

BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

VENV_DIR="$BACKEND_DIR/.venv"
VENV_BIN="$VENV_DIR/bin"
VENV_PYTHON="$VENV_BIN/python"
BACKEND_REQUIREMENTS_FILE="$BACKEND_DIR/requirements.txt"
BACKEND_REQUIREMENTS_STAMP="$VENV_DIR/.requirements.sha256"
BACKEND_CMD_PATTERN="$VENV_PYTHON -m uvicorn app.main:app"

FRONTEND_PACKAGE_FILE="$FRONTEND_DIR/package.json"
FRONTEND_LOCK_FILE="$FRONTEND_DIR/package-lock.json"
FRONTEND_NODE_MODULES="$FRONTEND_DIR/node_modules"
FRONTEND_VITE_BIN="$FRONTEND_NODE_MODULES/.bin/vite"
FRONTEND_PACKAGE_STAMP="$FRONTEND_NODE_MODULES/.package.sha256"
FRONTEND_CMD_PATTERN="$FRONTEND_VITE_BIN"

BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
STATUS_FILE="$RUN_DIR/status.env"

BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

BOOTSTRAP_COMPLETE=0
SERVICES_SPAWNED=0
PRESERVE_RUNNING_SERVICES=0
BACKEND_PID=""
FRONTEND_PID=""

say() {
  printf '[start] %s\n' "$1"
}

fail() {
  printf '[start] ERROR: %s\n' "$1" >&2
  exit 1
}

is_pid_running() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1

  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  lsof -a -p "$pid" -d cwd -Fn >/dev/null 2>&1
}

read_pid() {
  local file="$1"
  [[ -f "$file" ]] && tr -d '[:space:]' <"$file" || true
}

pid_matches() {
  local pid="$1"
  local pattern="$2"
  local expected_cwd="$3"
  local expected_log="$4"
  local command cwd

  if ! is_pid_running "$pid"; then
    return 1
  fi

  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ -n "$command" && "$command" == *"$pattern"* ]]; then
    return 0
  fi

  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  if [[ -z "$cwd" || "$cwd" != "$expected_cwd" ]]; then
    return 1
  fi

  lsof -a -p "$pid" -d 1,2 -Fn 2>/dev/null | grep -F "n$expected_log" >/dev/null 2>&1
}

backend_pid_matches() {
  local pid="$1"
  pid_matches "$pid" "$BACKEND_CMD_PATTERN" "$BACKEND_DIR" "$BACKEND_LOG"
}

frontend_pid_matches() {
  local pid="$1"
  pid_matches "$pid" "$FRONTEND_CMD_PATTERN" "$FRONTEND_DIR" "$FRONTEND_LOG"
}

cleanup_runtime_files() {
  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE" "$STATUS_FILE"
}

stop_pid_if_running() {
  local pid="$1"
  local service="$2"

  if [[ -z "$pid" ]] || ! is_pid_running "$pid"; then
    return 0
  fi

  case "$service" in
    backend)
      backend_pid_matches "$pid" || return 0
      ;;
    frontend)
      frontend_pid_matches "$pid" || return 0
      ;;
    *)
      fail "Unknown service '$service'"
      ;;
  esac

  if ! is_pid_running "$pid"; then
    return 0
  fi

  kill "$pid" 2>/dev/null || true
  for _ in {1..10}; do
    if ! is_pid_running "$pid"; then
      return 0
    fi
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
}

cleanup_on_exit() {
  rm -rf "$LOCK_DIR"
  if [[ "$BOOTSTRAP_COMPLETE" -eq 1 || "$PRESERVE_RUNNING_SERVICES" -eq 1 ]]; then
    return 0
  fi

  stop_pid_if_running "$BACKEND_PID" "backend"
  stop_pid_if_running "$FRONTEND_PID" "frontend"
  cleanup_runtime_files
}

handle_interrupt() {
  local signal="$1"

  if [[ "$SERVICES_SPAWNED" -eq 1 ]]; then
    PRESERVE_RUNNING_SERVICES=1
    [[ -f "$STATUS_FILE" ]] || write_status_file
    say "Startup interrupted by $signal. Leaving services running in the background."
    say "Frontend: $FRONTEND_URL"
    say "Backend:  $BACKEND_URL"
    say "Logs:     $BACKEND_LOG, $FRONTEND_LOG"
    exit 130
  fi

  say "Startup interrupted by $signal. Cleaning up partial startup."
  exit 130
}

trap 'handle_interrupt INT' INT
trap 'handle_interrupt TERM' TERM
trap cleanup_on_exit EXIT

require_tool() {
  local tool="$1"
  command -v "$tool" >/dev/null 2>&1 || fail "Missing required tool: $tool"
}

compute_hash() {
  python3 - "$@" <<'PY'
import hashlib
import pathlib
import sys

hasher = hashlib.sha256()
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    if not path.exists():
        continue
    hasher.update(path.name.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(path.read_bytes())
    hasher.update(b"\0")
print(hasher.hexdigest())
PY
}

find_listening_pid() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

load_env() {
  [[ -f "$ENV_FILE" ]] || fail "Missing .env file. Copy .env.example to .env and fill in your database credentials."
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a

  BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
  BACKEND_PORT="${BACKEND_PORT:-8000}"
  FRONTEND_HOST="${FRONTEND_HOST:-$BACKEND_HOST}"
  FRONTEND_PORT="${FRONTEND_PORT:-3000}"
  DATABASE_URL="${DATABASE_URL:-}"

  [[ -n "$DATABASE_URL" ]] || fail "DATABASE_URL is not set in .env"

  BACKEND_URL="http://$BACKEND_HOST:$BACKEND_PORT"
  FRONTEND_URL="http://$FRONTEND_HOST:$FRONTEND_PORT"
}

preflight_settings() {
  say "Validating startup config."

  if [[ "$DATABASE_URL" != postgresql+asyncpg://* ]]; then
    fail "DATABASE_URL must start with postgresql+asyncpg://. Update .env, for example: DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/futuresbot"
  fi

  "$VENV_PYTHON" - <<'PY'
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "backend"))

try:
    from app.core.config import get_settings
    settings = get_settings()
    if not settings.cors_origins:
        raise ValueError("CORS_ORIGINS resolved to an empty list")
except Exception as exc:
    print(f"Config validation failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

ensure_single_start() {
  mkdir -p "$RUN_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "Another startup is already in progress. If it is stale, run ./stop.sh first."
  fi
}

ensure_ports_free() {
  local port="$1"
  local label="$2"
  local occupant

  occupant="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$occupant" ]]; then
    fail "$label port $port is already in use by PID $occupant. Stop that process or run ./stop.sh if it belongs to this project."
  fi
}

write_status_file() {
  : >"$STATUS_FILE"
  {
    printf 'PROJECT_ROOT=%q\n' "$ROOT_DIR"
    printf 'STARTED_AT=%q\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'BACKEND_HOST=%q\n' "$BACKEND_HOST"
    printf 'BACKEND_PORT=%q\n' "$BACKEND_PORT"
    printf 'FRONTEND_HOST=%q\n' "$FRONTEND_HOST"
    printf 'FRONTEND_PORT=%q\n' "$FRONTEND_PORT"
    printf 'BACKEND_URL=%q\n' "$BACKEND_URL"
    printf 'FRONTEND_URL=%q\n' "$FRONTEND_URL"
    printf 'BACKEND_PID=%q\n' "$BACKEND_PID"
    printf 'FRONTEND_PID=%q\n' "$FRONTEND_PID"
    printf 'BACKEND_LOG=%q\n' "$BACKEND_LOG"
    printf 'FRONTEND_LOG=%q\n' "$FRONTEND_LOG"
    printf 'BACKEND_CMD_PATTERN=%q\n' "$BACKEND_CMD_PATTERN"
    printf 'FRONTEND_CMD_PATTERN=%q\n' "$FRONTEND_CMD_PATTERN"
  } >>"$STATUS_FILE"
}

check_already_running() {
  local recorded_backend_pid
  local recorded_frontend_pid

  recorded_backend_pid="$(read_pid "$BACKEND_PID_FILE")"
  recorded_frontend_pid="$(read_pid "$FRONTEND_PID_FILE")"

  if [[ -z "$recorded_backend_pid" && -z "$recorded_frontend_pid" ]]; then
    return 1
  fi

  if backend_pid_matches "$recorded_backend_pid" \
    && frontend_pid_matches "$recorded_frontend_pid" \
    && curl -fsS "$BACKEND_URL/api/status" >/dev/null 2>&1 \
    && curl -fsS "$FRONTEND_URL" >/dev/null 2>&1; then
    BACKEND_PID="$recorded_backend_pid"
    FRONTEND_PID="$recorded_frontend_pid"
    SERVICES_SPAWNED=1
    PRESERVE_RUNNING_SERVICES=1
    say "Project is already running."
    say "Frontend: $FRONTEND_URL"
    say "Backend:  $BACKEND_URL"
    exit 0
  fi

  say "Found stale or partial runtime state. Cleaning it before startup."
  stop_pid_if_running "$recorded_backend_pid" "backend"
  stop_pid_if_running "$recorded_frontend_pid" "frontend"
  cleanup_runtime_files
  return 1
}

check_discovered_running_services() {
  local discovered_backend_pid
  local discovered_frontend_pid

  discovered_backend_pid="$(find_listening_pid "$BACKEND_PORT")"
  discovered_frontend_pid="$(find_listening_pid "$FRONTEND_PORT")"

  if [[ -n "$discovered_backend_pid" ]] && ! backend_pid_matches "$discovered_backend_pid"; then
    discovered_backend_pid=""
  fi
  if [[ -n "$discovered_frontend_pid" ]] && ! frontend_pid_matches "$discovered_frontend_pid"; then
    discovered_frontend_pid=""
  fi

  if [[ -z "$discovered_backend_pid" && -z "$discovered_frontend_pid" ]]; then
    return 1
  fi

  if [[ -n "$discovered_backend_pid" && -n "$discovered_frontend_pid" ]] \
    && curl -fsS "$BACKEND_URL/api/status" >/dev/null 2>&1 \
    && curl -fsS "$FRONTEND_URL" >/dev/null 2>&1; then
    BACKEND_PID="$discovered_backend_pid"
    FRONTEND_PID="$discovered_frontend_pid"
    printf '%s\n' "$BACKEND_PID" >"$BACKEND_PID_FILE"
    printf '%s\n' "$FRONTEND_PID" >"$FRONTEND_PID_FILE"
    SERVICES_SPAWNED=1
    PRESERVE_RUNNING_SERVICES=1
    write_status_file
    say "Project is already running."
    say "Frontend: $FRONTEND_URL"
    say "Backend:  $BACKEND_URL"
    exit 0
  fi

  say "Found project services without a healthy runtime state. Cleaning them before startup."
  stop_pid_if_running "$discovered_backend_pid" "backend"
  stop_pid_if_running "$discovered_frontend_pid" "frontend"
  cleanup_runtime_files
  return 1
}

prepare_backend() {
  local current_hash stored_hash

  current_hash="$(compute_hash "$BACKEND_REQUIREMENTS_FILE")"
  if [[ ! -d "$VENV_DIR" ]]; then
    say "Creating backend virtual environment."
    python3 -m venv "$VENV_DIR"
    "$VENV_PYTHON" -m pip install --upgrade pip >/dev/null
  fi

  stored_hash="$( [[ -f "$BACKEND_REQUIREMENTS_STAMP" ]] && cat "$BACKEND_REQUIREMENTS_STAMP" || true )"
  if [[ ! -x "$VENV_PYTHON" || "$stored_hash" != "$current_hash" ]]; then
    say "Installing backend dependencies."
    "$VENV_PYTHON" -m pip install -r "$BACKEND_REQUIREMENTS_FILE"
    printf '%s\n' "$current_hash" >"$BACKEND_REQUIREMENTS_STAMP"
  fi
}

prepare_frontend() {
  local current_hash stored_hash

  current_hash="$(compute_hash "$FRONTEND_PACKAGE_FILE" "$FRONTEND_LOCK_FILE")"
  stored_hash="$( [[ -f "$FRONTEND_PACKAGE_STAMP" ]] && cat "$FRONTEND_PACKAGE_STAMP" || true )"

  if [[ ! -d "$FRONTEND_NODE_MODULES" || ! -x "$FRONTEND_VITE_BIN" || "$stored_hash" != "$current_hash" ]]; then
    say "Installing frontend dependencies."
    (
      cd "$FRONTEND_DIR"
      npm install
    )
    printf '%s\n' "$current_hash" >"$FRONTEND_PACKAGE_STAMP"
  fi
}

check_database_auth() {
  say "Checking PostgreSQL connectivity from .env."
  DATABASE_URL="$DATABASE_URL" "$VENV_PYTHON" - <<'PY'
import asyncio
import os
import socket
import sys
from urllib.parse import unquote, urlsplit

import asyncpg

database_url = os.environ["DATABASE_URL"].replace("+asyncpg", "")
parsed = urlsplit(database_url)
host = parsed.hostname or "localhost"
port = parsed.port or 5432
database_name = parsed.path.lstrip("/") or "<unknown>"
username = unquote(parsed.username or "")

try:
    with socket.create_connection((host, port), timeout=3):
        pass
except OSError as exc:
    print(
        f"PostgreSQL is not reachable at {host}:{port} for database '{database_name}'. "
        f"Start PostgreSQL or update DATABASE_URL in .env. ({exc})",
        file=sys.stderr,
    )
    raise SystemExit(1)

async def main() -> None:
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("SELECT 1")
    finally:
        await conn.close()

try:
    asyncio.run(main())
except Exception as exc:  # pragma: no cover - shell-facing path
    print(
        f"PostgreSQL rejected the connection for user '{username or '<unknown>'}' "
        f"to database '{database_name}' at {host}:{port}. "
        f"Check the username, password, and that the database exists. ({exc})",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

run_migrations() {
  say "Running database migrations."
  (
    cd "$BACKEND_DIR"
    "$VENV_PYTHON" -m alembic upgrade head
  )
}

start_backend() {
  say "Starting backend."
  : >"$BACKEND_LOG"
  (
    cd "$BACKEND_DIR"
    nohup "$VENV_PYTHON" -m uvicorn app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" >>"$BACKEND_LOG" 2>&1 &
    printf '%s\n' "$!" >"$BACKEND_PID_FILE"
  )
  BACKEND_PID="$(read_pid "$BACKEND_PID_FILE")"
}

start_frontend() {
  say "Starting frontend."
  : >"$FRONTEND_LOG"
  (
    cd "$FRONTEND_DIR"
    nohup "$FRONTEND_VITE_BIN" --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" >>"$FRONTEND_LOG" 2>&1 &
    printf '%s\n' "$!" >"$FRONTEND_PID_FILE"
  )
  FRONTEND_PID="$(read_pid "$FRONTEND_PID_FILE")"
  if [[ -n "$BACKEND_PID" && -n "$FRONTEND_PID" ]]; then
    SERVICES_SPAWNED=1
  fi
}

wait_for_http() {
  local name="$1"
  local url="$2"
  local pid="$3"
  local log_file="$4"
  local timeout="${5:-60}"

  for ((i = 1; i <= timeout; i++)); do
    if ! is_pid_running "$pid"; then
      fail "$name exited before becoming ready. Check $log_file"
    fi
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  fail "$name did not become ready within ${timeout}s. Check $log_file"
}

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "$FRONTEND_URL" >/dev/null 2>&1 || true
  fi
}

main() {
  require_tool python3
  require_tool npm
  require_tool curl
  require_tool lsof

  mkdir -p "$RUN_DIR" "$LOG_DIR"
  load_env
  ensure_single_start
  check_already_running || true
  check_discovered_running_services || true

  ensure_ports_free "$BACKEND_PORT" "Backend"
  ensure_ports_free "$FRONTEND_PORT" "Frontend"

  prepare_backend
  prepare_frontend
  preflight_settings
  check_database_auth
  run_migrations
  start_backend
  start_frontend
  write_status_file

  wait_for_http "Backend" "$BACKEND_URL/api/status" "$BACKEND_PID" "$BACKEND_LOG" 60
  wait_for_http "Frontend" "$FRONTEND_URL" "$FRONTEND_PID" "$FRONTEND_LOG" 60

  BOOTSTRAP_COMPLETE=1
  open_browser

  say "Project is ready."
  say "Frontend: $FRONTEND_URL"
  say "Backend:  $BACKEND_URL"
  say "Logs:     $BACKEND_LOG, $FRONTEND_LOG"
}

main "$@"
