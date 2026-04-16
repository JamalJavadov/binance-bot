#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$ROOT_DIR/.env"
RUN_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/logs"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
STATUS_FILE="$RUN_DIR/status.env"
LOCK_DIR="$RUN_DIR/start.lock"
BACKEND_VENV_PYTHON="$ROOT_DIR/backend/.venv/bin/python"
FRONTEND_VITE_BIN="$ROOT_DIR/frontend/node_modules/.bin/vite"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

BACKEND_CMD_PATTERN="uvicorn app.main:app"
FRONTEND_CMD_PATTERN="vite"

say() {
  printf '[stop] %s\n' "$1"
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

cleanup_runtime() {
  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE" "$STATUS_FILE"
  rm -rf "$LOCK_DIR"
  rmdir "$RUN_DIR" 2>/dev/null || true
}

load_status_patterns() {
  local default_backend_pattern="$BACKEND_CMD_PATTERN"
  local default_frontend_pattern="$FRONTEND_CMD_PATTERN"

  if [[ -f "$STATUS_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$STATUS_FILE"
  fi
  if [[ ( -z "${BACKEND_PORT:-}" || -z "${FRONTEND_PORT:-}" ) && -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi

  BACKEND_CMD_PATTERN="${BACKEND_CMD_PATTERN:-$default_backend_pattern}"
  FRONTEND_CMD_PATTERN="${FRONTEND_CMD_PATTERN:-$default_frontend_pattern}"
  BACKEND_PORT="${BACKEND_PORT:-8000}"
  FRONTEND_PORT="${FRONTEND_PORT:-3000}"
}

find_listening_pid() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

terminate_pid() {
  local label="$1"
  local pid="$2"

  if [[ -z "$pid" ]]; then
    return 1
  fi

  if ! is_pid_running "$pid"; then
    say "$label is already stopped."
    return 1
  fi

  case "$label" in
    backend)
      if ! backend_pid_matches "$pid"; then
        say "Skipping PID $pid for $label because it no longer matches the managed backend service."
        return 1
      fi
      ;;
    frontend)
      if ! frontend_pid_matches "$pid"; then
        say "Skipping PID $pid for $label because it no longer matches the managed frontend service."
        return 1
      fi
      ;;
    *)
      say "Skipping unknown service label '$label'."
      return 1
      ;;
  esac

  say "Stopping $label (PID $pid)."
  kill "$pid" 2>/dev/null || true

  for _ in {1..15}; do
    if ! is_pid_running "$pid"; then
      say "$label stopped."
      return 0
    fi
    sleep 1
  done

  say "$label did not stop gracefully. Forcing termination."
  kill -9 "$pid" 2>/dev/null || true
  return 0
}

main() {
  local backend_pid frontend_pid stopped_any=0

  load_status_patterns
  backend_pid="$(read_pid "$BACKEND_PID_FILE")"
  frontend_pid="$(read_pid "$FRONTEND_PID_FILE")"

  if [[ -z "$backend_pid" ]] || ! backend_pid_matches "$backend_pid"; then
    backend_pid="$(find_listening_pid "$BACKEND_PORT")"
    if [[ -n "$backend_pid" ]] && ! backend_pid_matches "$backend_pid"; then
      backend_pid=""
    fi
  fi
  if [[ -z "$frontend_pid" ]] || ! frontend_pid_matches "$frontend_pid"; then
    frontend_pid="$(find_listening_pid "$FRONTEND_PORT")"
    if [[ -n "$frontend_pid" ]] && ! frontend_pid_matches "$frontend_pid"; then
      frontend_pid=""
    fi
  fi

  if terminate_pid "backend" "$backend_pid"; then
    stopped_any=1
  fi
  if terminate_pid "frontend" "$frontend_pid"; then
    stopped_any=1
  fi

  cleanup_runtime

  if [[ "$stopped_any" -eq 0 ]]; then
    say "Project is already stopped."
  else
    say "Project runtime cleanup is complete."
  fi
}

main "$@"
