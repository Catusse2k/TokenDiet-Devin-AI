#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
error() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || error "python3 not found. Install Python 3.10+ and retry."
python3 - <<'PY' || error "Python 3.10 or newer is required."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY

if [[ ! -d .venv ]]; then
  info "Creating virtualenv in .venv"
  python3 -m venv .venv
fi

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
PIP_BIN="$ROOT_DIR/.venv/bin/pip"
UVICORN_BIN="$ROOT_DIR/.venv/bin/uvicorn"

info "Upgrading pip and installing dependencies"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PIP_BIN" install -r requirements.txt

if [[ ! -f .env ]]; then
  info "Creating .env from .env.example"
  cp .env.example .env
  warn "Configure API keys in .env before using economy/flagship tiers."
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  info "Starting Redis via Docker Compose"
  docker compose up -d redis
else
  warn "Docker unavailable or not running: the router will automatically use the local JSON cache."
  warn "For local Redis without Docker set REDIS_URL and CACHE_BACKEND=redis in .env."
fi

HOST="${ROUTER_HOST:-127.0.0.1}"
PORT="${ROUTER_PORT:-8000}"
if [[ -f .env ]]; then
  HOST="$(grep -E '^ROUTER_HOST=' .env | tail -n1 | cut -d= -f2- | tr -d '\r' || true)"
  PORT="$(grep -E '^ROUTER_PORT=' .env | tail -n1 | cut -d= -f2- | tr -d '\r' || true)"
  HOST="${HOST:-$HOST}"
  PORT="${PORT:-$PORT}"
fi

info "Starting router on http://${HOST}:${PORT}"
info "Endpoint: POST /v1/chat/completions - GET /health - GET /stats"
exec "$UVICORN_BIN" router.main:app --host "$HOST" --port "$PORT"
