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



Here’s the simplest way to do it in **MongoDB Compass → Aggregation tab**.

If you want to list **all distinct `document_type` values for a given `repo_id`**, you can add these stages:

---

## ✅ **1. `$match` stage**

Filters to only the repo you care about.

Example:

```json
{
  "repo_id": 12345
}
```

Add this stage in Compass:

1. Click **"Add Stage"**
2. Select **`$match`**
3. Paste the filter:

```json
{
  "repo_id": 12345
}
```

---

## ✅ **2. `$group` stage**

Groups everything by repo and collects all unique document types.

Add another stage:

1. Click **"Add Stage"**
2. Select **`$group`**
3. Use:

```json
{
  "_id": "$repo_id",
  "document_types": { "$addToSet": "$document_type" }
}
```

---

## ✔️ Result you get:

```json
{
  "_id": 12345,
  "document_types": [
    "typeA",
    "typeB",
    "typeC"
  ]
}
```

---

## Optional: **Sort document types alphabetically**

Add another stage:

### `$project`:

```json
{
  "_id": 0,
  "document_types": {
    "$sortArray": { "input": "$document_types", "sortBy": 1 }
  }
}
```

---

## Final pipeline (copy/paste into Compass)

```json
[
  {
    "$match": { "repo_id": 12345 }
  },
  {
    "$group": {
      "_id": "$repo_id",
      "document_types": { "$addToSet": "$document_type" }
    }
  }
]
```

---

If you want the documents grouped differently (e.g., list repo_ids with all document types), let me know!
