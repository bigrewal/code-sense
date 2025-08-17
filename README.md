# Code Repo QA Agent

## Setup
1. Set up .env with GROQ_API_KEY="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"
2. Use UV: uv venv; source .venv/bin/activate; uv add fastapi uvicorn groq python-dotenv pydantic
3. Run: uvicorn app.main:app --reload
4. Test: POST to /query with {"question": "test"}

uv pip install -e ../tree-sitter-reference-resolver