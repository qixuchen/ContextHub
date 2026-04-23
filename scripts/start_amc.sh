#!/usr/bin/env bash
set -euo pipefail

# Start required local services (PostgreSQL + Neo4j) then run AMC API.
# Usage:
#   bash scripts/start_amc.sh
# Optional env:
#   AMC_HOST=127.0.0.1 AMC_PORT=8000 AMC_RELOAD=1 bash scripts/start_amc.sh
#   AMC_INSTALL_DEPS=0 bash scripts/start_amc.sh   # skip pip bootstrap
#   AMC_SERVICE_START_TIMEOUT_SEC=120 AMC_SERVICE_START_POLL_SEC=2 bash scripts/start_amc.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

load_dotenv_if_present() {
  if [[ -f ".env" ]]; then
    # shellcheck disable=SC1091
    set -a && source ".env" && set +a
  fi
}

run_with_sudo_if_needed() {
  if [[ "$EUID" -eq 0 ]]; then
    "$@"
    return 0
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    echo "[AMC] ERROR: sudo is required to start system service '$*'" >&2
    exit 1
  fi
  if sudo -n true >/dev/null 2>&1; then
    sudo -n "$@"
    return 0
  fi
  if [[ -n "${AMC_SUDO_PASSWORD:-}" ]]; then
    # Allow non-interactive startup by reading sudo password from env/.env.
    # NOTE: this is less secure than sudoers NOPASSWD; use only for local dev.
    printf '%s\n' "$AMC_SUDO_PASSWORD" | sudo -S -p '' "$@"
    return 0
  fi
  echo "[AMC] ERROR: sudo password required. Set AMC_SUDO_PASSWORD in .env or configure passwordless sudo." >&2
  exit 1
}

cleanup_stale_neo4j_pidfile_if_needed() {
  local pid_file="/var/lib/neo4j/run/neo4j.pid"
  local pid=""
  local cmd=""
  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi

  pid="$(tr -d '[:space:]' < "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    echo "[AMC] removing empty neo4j pid file: ${pid_file}"
    run_with_sudo_if_needed rm -f "$pid_file"
    return 0
  fi

  cmd="$(ps -p "$pid" -o cmd= 2>/dev/null || true)"
  if [[ -z "$cmd" ]]; then
    echo "[AMC] removing stale neo4j pid file: ${pid_file} (pid ${pid} not running)"
    run_with_sudo_if_needed rm -f "$pid_file"
    return 0
  fi
  if [[ "$cmd" == *neo4j* || "$cmd" == *org.neo4j* ]]; then
    return 0
  fi

  echo "[AMC] removing stale neo4j pid file: ${pid_file} (pid ${pid} belongs to: ${cmd})"
  run_with_sudo_if_needed rm -f "$pid_file"
}

load_dotenv_if_present

AMC_HOST="${AMC_HOST:-127.0.0.1}"
AMC_PORT="${AMC_PORT:-8000}"
AMC_RELOAD="${AMC_RELOAD:-1}"
AMC_INSTALL_DEPS="${AMC_INSTALL_DEPS:-1}"
AMC_SERVICE_START_TIMEOUT_SEC="${AMC_SERVICE_START_TIMEOUT_SEC:-90}"
AMC_SERVICE_START_POLL_SEC="${AMC_SERVICE_START_POLL_SEC:-2}"
PYTHON_BIN="python"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

ensure_python_env() {
  if [[ ! -x ".venv/bin/python" ]]; then
    echo "[AMC] creating virtualenv at .venv ..."
    "$PYTHON_BIN" -m venv .venv
  fi

  # Keep behavior aligned with README setup steps.
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
  python -m pip install -U pip

  if [[ "$AMC_INSTALL_DEPS" != "1" ]]; then
    echo "[AMC] skip dependency bootstrap (AMC_INSTALL_DEPS=0)"
    return 0
  fi

  if ! python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    echo "[AMC] installing project dependencies into .venv ..."
    python -m pip install -e ".[dev]"
  fi
}

check_port_open() {
  local port="$1"
  "$PYTHON_BIN" - "$port" <<'PY'
import socket
import sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(0.6)
try:
    s.connect(("127.0.0.1", port))
    ok = True
except Exception:
    ok = False
finally:
    s.close()
print("1" if ok else "0")
PY
}

start_service_if_needed() {
  local service_name="$1"
  local port="$2"
  local waited=0

  if [[ "$(check_port_open "$port")" == "1" ]]; then
    echo "[AMC] ${service_name} already listening on :${port}"
    return 0
  fi

  if [[ "$service_name" == "neo4j" ]]; then
    cleanup_stale_neo4j_pidfile_if_needed
  fi

  echo "[AMC] starting ${service_name} ..."
  if command -v systemctl >/dev/null 2>&1; then
    run_with_sudo_if_needed systemctl start "$service_name"
  else
    run_with_sudo_if_needed service "$service_name" start
  fi

  while [[ "$waited" -lt "$AMC_SERVICE_START_TIMEOUT_SEC" ]]; do
    if [[ "$(check_port_open "$port")" == "1" ]]; then
      echo "[AMC] ${service_name} is ready on :${port}"
      return 0
    fi
    sleep "$AMC_SERVICE_START_POLL_SEC"
    waited=$((waited + AMC_SERVICE_START_POLL_SEC))
  done

  echo "[AMC] ERROR: ${service_name} is still not listening on :${port} after ${AMC_SERVICE_START_TIMEOUT_SEC}s" >&2
  if command -v systemctl >/dev/null 2>&1; then
    echo "[AMC] ${service_name} status (systemctl):" >&2
    systemctl --no-pager --full status "$service_name" >&2 || true
  fi
  exit 1
}

ensure_python_env
PYTHON_BIN=".venv/bin/python"
start_service_if_needed "postgresql" "5432"
start_service_if_needed "neo4j" "7687"

UVICORN_BIN=".venv/bin/uvicorn"

if [[ "$AMC_RELOAD" == "1" ]]; then
  exec "$UVICORN_BIN" main:app --app-dir src --host "$AMC_HOST" --port "$AMC_PORT" --reload
else
  exec "$UVICORN_BIN" main:app --app-dir src --host "$AMC_HOST" --port "$AMC_PORT"
fi

