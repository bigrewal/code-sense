# Code Repo QA Agent

## Setup
1. Set up .env with GROQ_API_KEY="<>"
2. Use UV: uv venv; source .venv/bin/activate; uv add fastapi uvicorn groq python-dotenv pydantic
3. Run: uvicorn app.main:app --reload
4. Test: POST to /query with {"question": "test"}

uv pip install -e ../tree-sitter-reference-resolver

uv add --editable ../attentiondb

#### Purge Neo4j DB
MATCH (n)-[r]-() DELETE r
MATCH (n) DELETE n

CALL {
  MATCH ()-[r]-()
  RETURN r
  LIMIT 100000
}
DELETE r;

CALL {
  MATCH (r)
  RETURN r
  LIMIT 500000
}
DELETE r;


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
