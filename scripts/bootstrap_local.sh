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
# --- ensure env file ---
if [[ ! -f .env.local ]]; then
  if [[ -f .env.local.example ]]; then
    cp .env.local.example .env.local
    warn "Created .env.local from .env.local.example. Edit it (XAI_API_KEY)."
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

mkdir -p data
bold "SQLite datastore will be created at ${SQLITE_DB_PATH:-data/code_sense.sqlite3} on API startup."
