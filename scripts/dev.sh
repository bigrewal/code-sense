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

if [[ ! -f "$ROOT_DIR/.env.local" ]]; then
  echo "error: $ROOT_DIR/.env.local is missing." >&2
  echo "       cp $ROOT_DIR/.env.local.example $ROOT_DIR/.env.local and set your API key." >&2
  exit 1
fi

echo "==> syncing backend deps"
(cd "$BACKEND_DIR" && uv sync --quiet)

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo "==> installing frontend deps"
  (cd "$FRONTEND_DIR" && npm install --silent --no-audit --no-fund)
fi

find_free_port() {
  uv run --project "$BACKEND_DIR" python - "$1" <<'PY'
import socket
import sys

def can_bind(port: int) -> bool:
    targets = [(socket.AF_INET, "127.0.0.1")]
    if socket.has_ipv6:
        targets.append((socket.AF_INET6, "::1"))

    sockets = []
    try:
        for family, host in targets:
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                sock.close()
                return False
            sockets.append(sock)
        return True
    finally:
        for sock in sockets:
            sock.close()

start = int(sys.argv[1])
for port in range(start, start + 50):
    if not can_bind(port):
        continue
    print(port)
    break
else:
    raise SystemExit(f"no available port found from {start} to {start + 49}")
PY
}

BACKEND_PORT="$(find_free_port 8000)"
FRONTEND_PORT="$(find_free_port 5173)"

if [[ "$BACKEND_PORT" != "8000" ]]; then
  echo "==> backend port 8000 is busy; using http://localhost:$BACKEND_PORT"
fi

if [[ "$FRONTEND_PORT" != "5173" ]]; then
  echo "==> frontend port 5173 is busy; using http://localhost:$FRONTEND_PORT"
fi

DEV_ALLOWED_ORIGINS="http://localhost:$FRONTEND_PORT,http://127.0.0.1:$FRONTEND_PORT,http://localhost:$BACKEND_PORT,http://127.0.0.1:$BACKEND_PORT"

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

echo "==> starting backend on http://localhost:$BACKEND_PORT"
(
  cd "$ROOT_DIR"
  exec env PYTHONPATH="$BACKEND_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    ALLOWED_ORIGINS="$DEV_ALLOWED_ORIGINS" \
    uv run --project "$BACKEND_DIR" uvicorn app.main:app \
      --host 127.0.0.1 \
      --port "$BACKEND_PORT" \
      --env-file "$ROOT_DIR/.env.local"
) &
backend_pid=$!

echo "==> starting frontend on http://localhost:$FRONTEND_PORT"
(
  cd "$FRONTEND_DIR"
  exec env VITE_API_BASE="http://localhost:$BACKEND_PORT" \
    npm run dev --silent -- --host 0.0.0.0 --port "$FRONTEND_PORT" --strictPort
) &
frontend_pid=$!

# Surface the first failure: if either child exits, tear the other down too.
is_running_child() {
  jobs -pr | grep -qx "$1"
}

while is_running_child "$backend_pid" && is_running_child "$frontend_pid"; do
  sleep 1
done

status=0
if ! is_running_child "$backend_pid"; then
  set +e
  wait "$backend_pid"
  status=$?
  set -e
  echo "==> backend exited with status $status" >&2
elif ! is_running_child "$frontend_pid"; then
  set +e
  wait "$frontend_pid"
  status=$?
  set -e
  echo "==> frontend exited with status $status" >&2
fi

exit "$status"
