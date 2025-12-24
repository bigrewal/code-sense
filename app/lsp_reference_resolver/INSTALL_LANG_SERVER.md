## Install Scala lang server
#### Remove old binary
rm ~/.local/bin/cs

#### Download the correct one for macOS ARM64
curl -fLo cs.gz https://github.com/coursier/coursier/releases/latest/download/cs-aarch64-apple-darwin.gz
gzip -d cs.gz
chmod +x cs
mv cs ~/.local/bin/

#### Reload path and install
source ~/.zprofile
cs setup -y
cs install metals
metals -v


## Install Java server

#### On Mac
brew install jdtls
brew install --cask temurin@17

#### On linux

sudo apt-get update
sudo apt-get install -y wget unzip


JDTLS_VER="latest" 

wget -O jdtls.tar.gz https://download.eclipse.org/jdtls/milestones/latest/jdt-language-server-latest.tar.gz
mkdir -p $HOME/.local/jdtls && tar -xzf jdtls.tar.gz -C $HOME/.local/jdtls --strip-components=1

cat <<'SH' > $HOME/.local/bin/jdtls
#!/usr/bin/env bash
JDTLS_HOME="${JDTLS_HOME:-$HOME/.local/jdtls}"
JAVA_BIN="${JAVA_BIN:-$(command -v java)}"
exec "$JAVA_BIN" \
  -Declipse.application=org.eclipse.jdt.ls.core.id1 \
  -Dosgi.bundles.defaultStartLevel=4 \
  -Declipse.product=org.eclipse.jdt.ls.core.product \
  -Dlog.protocol=true -Dlog.level=ALL \
  -Xms256m -Xmx2G \
  -jar "$JDTLS_HOME/plugins/org.eclipse.equinox.launcher_*.jar" \
  -configuration "$JDTLS_HOME/config_linux" \
  "$@"
SH
chmod +x $HOME/.local/bin/jdtls
export PATH="$HOME/.local/bin:$PATH"


## Install Rust Server

brew install rust-analyzer