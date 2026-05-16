#!/usr/bin/env bash
# Spin up CodeSense locally: backend (uvicorn) + frontend (vite). Press Ctrl+C
# to stop both. Run from anywhere — paths resolve relative to this script.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: '$1' is required but not installed" >&2
    exit 1
  }
}
require uv
require npm

if [[ ! -f "$BACKEND_DIR/.env.local" ]]; then
  echo "error: $BACKEND_DIR/.env.local is missing." >&2
  echo "       cp $BACKEND_DIR/.env.local.example $BACKEND_DIR/.env.local and set your API key." >&2
  exit 1
fi

echo "==> syncing backend deps"
(cd "$BACKEND_DIR" && uv sync --quiet)

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo "==> installing frontend deps"
  (cd "$FRONTEND_DIR" && npm install --silent --no-audit --no-fund)
fi

# Recursively find every descendant of the given PID.
descendants() {
  local parent=$1
  local children
  children=$(pgrep -P "$parent" 2>/dev/null || true)
  for child in $children; do
    echo "$child"
    descendants "$child"
  done
}

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "==> shutting down"
  # Both `uv run` and `npm run dev` fork actual workers (uvicorn / vite) into
  # children that don't always receive a SIGINT relayed from their parent. Walk
  # the descendant tree of THIS script and signal each PID directly.
  local pids
  pids=$(descendants $$ | sort -u)
  if [[ -n "$pids" ]]; then
    # First TERM — graceful for uvicorn/vite.
    kill -TERM $pids 2>/dev/null || true
    # Then a short grace period before KILL for anything still alive.
    for _ in 1 2 3 4 5; do
      sleep 0.3
      pids=$(descendants $$ | sort -u)
      [[ -z "$pids" ]] && break
    done
    [[ -n "$pids" ]] && kill -KILL $pids 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "==> starting backend on http://localhost:8000"
( cd "$BACKEND_DIR" && exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 ) &
backend_pid=$!

echo "==> starting frontend on http://localhost:5173"
( cd "$FRONTEND_DIR" && exec npm run dev --silent ) &
frontend_pid=$!

# Surface the first failure: if either child exits, tear the other down too.
wait -n "$backend_pid" "$frontend_pid"
