FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gzip \
    nodejs \
    npm \
    openjdk-17-jre-headless \
    tar \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN set -eux; \
    curl -fLo /usr/local/bin/cs https://git.io/coursier-cli; \
    chmod +x /usr/local/bin/cs; \
    cs install --install-dir /usr/local/bin metals

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) ra_url="https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-x86_64-unknown-linux-gnu.gz" ;; \
      arm64) ra_url="https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-aarch64-unknown-linux-gnu.gz" ;; \
      *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fLo /tmp/rust-analyzer.gz "$ra_url"; \
    gunzip /tmp/rust-analyzer.gz; \
    install -m 0755 /tmp/rust-analyzer /usr/local/bin/rust-analyzer; \
    rm -f /tmp/rust-analyzer

RUN set -eux; \
    mkdir -p /opt/jdtls; \
    wget -O /tmp/jdtls.tar.gz https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz; \
    tar -xzf /tmp/jdtls.tar.gz -C /opt/jdtls; \
    rm -f /tmp/jdtls.tar.gz; \
    cat <<'SH' > /usr/local/bin/jdtls
#!/usr/bin/env bash
set -euo pipefail
JDTLS_HOME="${JDTLS_HOME:-/opt/jdtls}"
JAVA_BIN="${JAVA_BIN:-/usr/bin/java}"
LAUNCHER_JAR="$(ls "$JDTLS_HOME"/plugins/org.eclipse.equinox.launcher_*.jar | head -n 1)"
exec "$JAVA_BIN" \
  -Declipse.application=org.eclipse.jdt.ls.core.id1 \
  -Dosgi.bundles.defaultStartLevel=4 \
  -Declipse.product=org.eclipse.jdt.ls.core.product \
  -Dlog.protocol=true -Dlog.level=ALL \
  -Xms256m -Xmx2G \
  -jar "$LAUNCHER_JAR" \
  -configuration "$JDTLS_HOME/config_linux" \
  "$@"
SH
RUN chmod +x /usr/local/bin/jdtls

RUN npm install -g typescript typescript-language-server

COPY . /app

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

RUN set -eux; \
    command -v jdtls; \
    command -v pylsp; \
    command -v rust-analyzer; \
    command -v metals; \
    command -v typescript-language-server; \
    pylsp --version; \
    rust-analyzer --version; \
    metals -v; \
    typescript-language-server --version

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
