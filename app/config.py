import os
from pathlib import Path

if Path(".env.local").exists():
    from dotenv import load_dotenv

    load_dotenv(".env.local")


class Config:
    XAI_API_KEY = os.getenv("XAI_API_KEY")
    GROK_4_NON_REASONING_MODEL = "grok-4-1-fast-non-reasoning"
    LLM_TEMPERATURE = 0.7
    LLM_MAX_TOKENS = 10240

    BASE_REPO_DIR: str = os.getenv("BASE_REPO_DIR", ".codesense/repos")

    REPO_BROWSER_ROOTS: list[str] = [
        root.strip()
        for root in os.getenv("REPO_BROWSER_ROOTS", str(Path.home())).split(",")
        if root.strip()
    ]
    REPO_BROWSER_MAX_ENTRIES: int = int(os.getenv("REPO_BROWSER_MAX_ENTRIES", "500"))

    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", ".codesense/code_sense.sqlite3")

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

    LOG_DB_QUERIES: bool = os.getenv("LOG_DB_QUERIES", "false").lower() == "true"

    ALLOWED_ORIGINS: list[str] = os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:8000"
    ).split(",")

    DB_OPERATION_TIMEOUT: int = int(os.getenv("DB_OPERATION_TIMEOUT", "30"))


def validate_required_settings() -> None:
    """Fail fast with a clear list of missing required environment values."""
    required = {
        "XAI_API_KEY": Config.XAI_API_KEY,
        "SQLITE_DB_PATH": Config.SQLITE_DB_PATH,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
