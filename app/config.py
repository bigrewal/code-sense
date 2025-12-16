import os
from pathlib import Path
from typing import Any, Dict

if Path(".env.local").exists():
    # Optional local-only overrides; production should rely on env vars.
    from dotenv import load_dotenv

    load_dotenv(".env.local")


class Config:
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    XAI_API_KEY = os.getenv("XAI_API_KEY")
    GPT_OSS_20GB_MODEL = "openai/gpt-oss-20b"
    GPT_OSS_120GB_MODEL = "openai/gpt-oss-120b"
    GROK_4_NON_REASONING_MODEL = "grok-4-1-fast-non-reasoning"
    GROK_4_REASONING_MODEL = "grok-4-1-fast-reasoning"
    LLM_TEMPERATURE = 0.7
    LLM_MAX_TOKENS = 10240

    # AWS S3 Configuration
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "eu-west-2"
    S3_BUCKET_NAME: str = "code-analysis-repos"

    # New Neo4j configs
    NEO4J_URI: str = os.getenv("NEO4J_URI")
    NEO4J_USER: str = os.getenv("NEO4J_USER")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD")

    # MongoDB Configuration
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB_NAME: str = "code_comprehension"

    IGNORE_FOLDERS: dict = {
        "test",
        "tests",
        "docs",
        "examples",
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "dist",
        "build",
        "target",
        ".venv",
        "venv",
        "__pycache__",
    }

    SUPPORTED_LANGUAGES: dict = {
        ".py": "python",
        ".java": "java",
        ".scala": "scala",
        ".rs": "rust",
    }

    min_supported_ratio: float = 0.5

def validate_required_settings() -> None:
    """Fail fast with a clear list of missing required environment values."""
    required = {
        "XAI_API_KEY": Config.XAI_API_KEY,
        "NEO4J_URI": Config.NEO4J_URI,
        "NEO4J_USER": Config.NEO4J_USER,
        "NEO4J_PASSWORD": Config.NEO4J_PASSWORD,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
