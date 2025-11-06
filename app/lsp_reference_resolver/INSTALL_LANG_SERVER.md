#### Install Scala lang server
# Remove old binary
rm ~/.local/bin/cs

# Download the correct one for macOS ARM64
curl -fLo cs.gz https://github.com/coursier/coursier/releases/latest/download/cs-aarch64-apple-darwin.gz
gzip -d cs.gz
chmod +x cs
mv cs ~/.local/bin/

# Reload path and install
source ~/.zprofile
cs setup -y
cs install metals
metals -v


##### Install Java server

