import os
from pathlib import Path
from typing import List

if Path(".env.local").exists():
    # Optional local-only overrides; production should rely on env vars.
    from dotenv import load_dotenv

    load_dotenv(".env.local")


class Config:
    XAI_API_KEY = os.getenv("XAI_API_KEY")
    GROK_4_NON_REASONING_MODEL = "grok-4-1-fast-non-reasoning"
    LLM_TEMPERATURE = 0.7
    LLM_MAX_TOKENS = 10240

    # Base directory to store cloned repositories
    BASE_REPO_DIR: str = "data"

    # SQLite Configuration
    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "data/code_sense.sqlite3")

    IGNORE_FOLDERS: set[str] = {
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
        ".tox",
        ".eggs",
        "site-packages",
    }

    SUPPORTED_LANGUAGES: dict[str, str] = {
        ".py": "python",
        ".java": "java",
        ".scala": "scala",
        ".rs": "rust",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".html": "html",
        ".htm": "html",
        ".go": "go",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".hpp": "cpp",
        ".hh": "cpp",
        ".hxx": "cpp",
        ".c": "c",
        ".h": "c",
        ".f": "fortran",
        ".for": "fortran",
        ".f77": "fortran",
        ".f90": "fortran",
        ".f95": "fortran",
        ".f03": "fortran",
        ".jl": "julia",
        ".m": "matlab",
        ".css": "css",
        ".agc": "assembly",
    }

    min_supported_cov_ratio: float = 0.5

    # ========== Security ==========

    # Disable query logging in production for security
    LOG_DB_QUERIES: bool = os.getenv("LOG_DB_QUERIES", "false").lower() == "true"

    # CORS allowed origins
    ALLOWED_ORIGINS: List[str] = os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:8000"
    ).split(",")

    DB_OPERATION_TIMEOUT: int = int(os.getenv("DB_OPERATION_TIMEOUT", "30"))  # 30s


def validate_required_settings() -> None:
    """Fail fast with a clear list of missing required environment values."""
    # Check required environment variables
    required = {
        "XAI_API_KEY": Config.XAI_API_KEY,
        "SQLITE_DB_PATH": Config.SQLITE_DB_PATH,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
