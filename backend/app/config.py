import os
from pathlib import Path

from dotenv import load_dotenv


def _load_local_env() -> None:
    """Load local dotenv files independent of the process working directory."""
    backend_dir = Path(__file__).resolve().parents[1]
    repo_root = backend_dir.parent
    candidates = (
        Path.cwd() / ".env.local",
        repo_root / ".env.local",
        backend_dir / ".env.local",
    )

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen or not candidate.exists():
            continue
        load_dotenv(candidate, override=False)
        seen.add(resolved)


_load_local_env()


class Config:
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "grok").lower()
    LLM_MODEL = os.getenv("LLM_MODEL", "")
    LLM_TEMPERATURE = 0.7
    LLM_MAX_TOKENS = 10240

    # xAI / Grok
    XAI_API_KEY = os.getenv("XAI_API_KEY")
    GROK_4_NON_REASONING_MODEL = "grok-4.3"

    # OpenAI (also used for OpenAI-compatible endpoints via OPENAI_BASE_URL)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")

    # Anthropic
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

    # AWS Bedrock (Claude)
    AWS_REGION = os.getenv("AWS_REGION", "")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN", "")

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


_PROVIDER_REQUIRED_VARS: dict[str, tuple[str, ...]] = {
    "grok": ("XAI_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "bedrock": ("AWS_REGION",),
}


def validate_required_settings() -> None:
    """Fail fast with a clear list of missing required environment values."""
    required: dict[str, str] = {"SQLITE_DB_PATH": Config.SQLITE_DB_PATH}
    provider = Config.LLM_PROVIDER
    if provider not in _PROVIDER_REQUIRED_VARS:
        raise RuntimeError(
            f"Unsupported LLM_PROVIDER={provider!r}. "
            f"Choose one of: {', '.join(_PROVIDER_REQUIRED_VARS)}."
        )
    for var in _PROVIDER_REQUIRED_VARS[provider]:
        required[var] = getattr(Config, var, "") or ""

    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables for LLM_PROVIDER={provider!r}: "
            f"{', '.join(missing)}"
        )
