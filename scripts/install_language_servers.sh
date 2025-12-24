#!/usr/bin/env bash
set -euo pipefail

# Installs language servers used by this repo:
# - Java: jdtls
# - Python: python-lsp-server (pylsp)
# - Rust: rust-analyzer
# - Scala: metals (via coursier)
#
# Usage:
#   ./scripts/install_language_servers.sh
#
# Notes:
# - Requires sudo on Linux for apt installs.
# - On macOS uses Homebrew.
# - Metals install uses coursier, installed under ~/.local/bin by default.

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
warn() { printf "\033[33mWARN:\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[31mERROR:\033[0m %s\n" "$*" >&2; exit 1; }

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

have() { command -v "$1" >/dev/null 2>&1; }

ensure_path_local_bin() {
  mkdir -p "$HOME/.local/bin"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
      export PATH="$HOME/.local/bin:$PATH"
      warn "Added ~/.local/bin to PATH for this session. Consider adding it to your shell profile."
      ;;
  esac
}


install_rust_analyzer_macos() {
  bold "Installing rust-analyzer (macOS)..."
  have brew || die "Homebrew not found. Install from https://brew.sh/"
  brew install rust-analyzer
}

install_rust_analyzer_linux() {
  bold "Installing rust-analyzer (Linux)..."
  if have apt-get; then
    sudo apt-get update
    # Some distros package rust-analyzer; if not available, fall back.
    if sudo apt-get install -y rust-analyzer; then
      return
    fi
    warn "rust-analyzer package not available via apt on this distro; falling back to rustup."
  fi

  if have rustup; then
    rustup component add rust-analyzer || true
    # Not all rustup channels ship it as a component; if missing, suggest manual.
    have rust-analyzer || warn "rust-analyzer not found after rustup; install via your distro or https://rust-analyzer.github.io/"
  else
    warn "rustup not found. Install rust-analyzer via your package manager or from https://rust-analyzer.github.io/"
  fi
}

install_jdtls_macos() {
  bold "Installing Java (Temurin 17) + jdtls (macOS)..."
  have brew || die "Homebrew not found. Install from https://brew.sh/"
  brew install jdtls
  # Temurin casks are versioned; this matches your note (temurin@17)
  brew install --cask temurin@17 || true
  have java || warn "java not found on PATH; you may need to restart your terminal."
}

install_jdtls_linux() {
  bold "Installing Java + jdtls (Linux)..."
  have apt-get || die "This script supports Linux via apt-get. Install jdtls manually for your distro."
  sudo apt-get update
  sudo apt-get install -y wget unzip default-jre

  # Install jdtls to ~/.local/jdtls and a wrapper to ~/.local/bin/jdtls
  ensure_path_local_bin
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' EXIT

  wget -O "$tmpdir/jdtls.tar.gz" https://download.eclipse.org/jdtls/milestones/latest/jdt-language-server-latest.tar.gz
  mkdir -p "$HOME/.local/jdtls"
  tar -xzf "$tmpdir/jdtls.tar.gz" -C "$HOME/.local/jdtls" --strip-components=1

  cat <<'SH' > "$HOME/.local/bin/jdtls"
#!/usr/bin/env bash
set -euo pipefail
JDTLS_HOME="${JDTLS_HOME:-$HOME/.local/jdtls}"
JAVA_BIN="${JAVA_BIN:-$(command -v java)}"
exec "$JAVA_BIN" \
  -Declipse.application=org.eclipse.jdt.ls.core.id1 \
  -Dosgi.bundles.defaultStartLevel=4 \
  -Declipse.product=org.eclipse.jdt.ls.core.product \
  -Dlog.protocol=true -Dlog.level=ALL \
  -Xms256m -Xmx2G \
  -jar "$JDTLS_HOME/plugins/org.eclipse.equinox.launcher_"*.jar \
  -configuration "$JDTLS_HOME/config_linux" \
  "$@"
SH
  chmod +x "$HOME/.local/bin/jdtls"
}

install_metals_macos() {
  bold "Installing Scala Metals via coursier (macOS)..."
  ensure_path_local_bin

  # Install coursier binary appropriate for macOS arch
  local cs_url=""
  if [[ "$ARCH" == "arm64" ]]; then
    cs_url="https://github.com/coursier/coursier/releases/latest/download/cs-aarch64-apple-darwin.gz"
  else
    cs_url="https://github.com/coursier/coursier/releases/latest/download/cs-x86_64-apple-darwin.gz"
  fi

  curl -fLo "$HOME/.local/bin/cs.gz" "$cs_url"
  gzip -df "$HOME/.local/bin/cs.gz"
  chmod +x "$HOME/.local/bin/cs"

  "$HOME/.local/bin/cs" setup -y
  "$HOME/.local/bin/cs" install metals
}

install_metals_linux() {
  bold "Installing Scala Metals via coursier (Linux)..."
  ensure_path_local_bin

  local cs_url=""
  if [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]]; then
    cs_url="https://github.com/coursier/coursier/releases/latest/download/cs-aarch64-pc-linux.gz"
  else
    cs_url="https://github.com/coursier/coursier/releases/latest/download/cs-x86_64-pc-linux.gz"
  fi

  curl -fLo "$HOME/.local/bin/cs.gz" "$cs_url"
  gzip -df "$HOME/.local/bin/cs.gz"
  chmod +x "$HOME/.local/bin/cs"

  "$HOME/.local/bin/cs" setup -y
  "$HOME/.local/bin/cs" install metals
}

verify() {
  bold "Verifying language servers on PATH..."
  local missing=0
  for cmd in jdtls pylsp rust-analyzer metals; do
    if have "$cmd"; then
      echo "✅ $cmd -> $(command -v "$cmd")"
    else
      echo "❌ missing: $cmd"
      missing=1
    fi
  done

  echo
  bold "Notes:"
  echo "- jdtls requires a workspace dir when launched by the app: jdtls -data <repo_path>"
  echo "- If you installed coursier/metals or jdtls to ~/.local/bin, ensure ~/.local/bin is on PATH in your shell profile."
  echo

  if [[ "$missing" -ne 0 ]]; then
    echo "ERROR: One or more required language servers are missing." >&2
    exit 1
  fi
}

main() {
  bold "Installing language servers for: $OS / $ARCH"
  echo

  case "$OS" in
    darwin)
      install_jdtls_macos
      install_metals_macos
      install_rust_analyzer_macos
      ;;
    linux)
      install_jdtls_linux
      install_metals_linux
      install_rust_analyzer_linux
      ;;
    *)
      die "Unsupported OS: $OS"
      ;;
  esac

  echo
  verify
  echo
  bold "Done."
}

main "$@"
