#!/usr/bin/env bash
set -euo pipefail

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
warn() { printf "\033[33mWARN:\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[31mERROR:\033[0m %s\n" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

[[ -f pyproject.toml ]] || {
  echo "ERROR: pyproject.toml not found. Run bootstrap from the repo root." >&2
  exit 1
}

# --- prerequisites (we skip installing uv, per request) ---
have uv || die "uv not found. Install uv first: https://docs.astral.sh/uv/"
have docker || die "docker not found."
docker compose version >/dev/null 2>&1 || die "docker compose not available."

# --- ensure env file ---
if [[ ! -f .env.local ]]; then
  if [[ -f .env.local.example ]]; then
    cp .env.local.example .env.local
    warn "Created .env.local from .env.local.example. Edit it (XAI_API_KEY, NEO4J_PASSWORD)."
  else
    die ".env.local missing and .env.local.example not found."
  fi
fi

# Load env for this shell (optional, but nice for checks)
set -a
# shellcheck disable=SC1091
source .env.local
set +a

# --- install language servers ---
bold "Installing language servers..."
bash "$ROOT_DIR/scripts/install_language_servers.sh"

# --- start datastores ---
if [[ -f docker-compose.yml ]]; then
  bold "Starting MongoDB + Neo4j (docker compose --env-file .env.local up -d)..."
  docker compose --env-file .env.local up -d
else
  warn "docker-compose.yml not found. Skipping datastore startup."
fi

# --- sync python deps ---
bold "Syncing Python dependencies (uv sync)..."
uv sync

# --- run api ---
bold "Starting API at http://localhost:8000 ..."
exec uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000