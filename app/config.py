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

    # MongoDB Configuration
    MONGO_URI: str = os.getenv("MONGO_URI")
    MONGO_DB_NAME: str = "code_comprehension"

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
        ".ts": "typescript",
        ".tsx": "typescript",
    }

    LANGUAGE_DEFINITION_MAP: dict[str, set[str]] = {
        "python": {"function_definition", "class_definition", "assignment"},
        "rust": {
            "struct_item", "enum_item", "union_item", "type_item",
            "function_item", "trait_item", "mod_item", "macro_definition"
        },
        "scala": {
            "package_clause", "trait_definition", "enum_definition",
            "simple_enum_case", "full_enum_case", "class_definition",
            "object_definition", "function_definition", "val_definition",
            "given_definition", "var_definition", "val_declaration",
            "var_declaration", "type_definition", "class_parameter"
        },
        "java": {
            "class_declaration", "method_declaration", "interface_declaration"
        },
        "typescript": {
            "function_declaration", "class_declaration", "interface_declaration",
            "type_alias_declaration", "lexical_declaration", "method_definition",
            "enum_declaration"
        }
    }

    min_supported_cov_ratio: float = 0.5

    # ========== Database Connection Pool Configuration ==========

    # MongoDB Connection Pool Settings
    MONGO_MAX_POOL_SIZE: int = int(os.getenv("MONGO_MAX_POOL_SIZE", "50"))
    MONGO_MIN_POOL_SIZE: int = int(os.getenv("MONGO_MIN_POOL_SIZE", "10"))
    MONGO_MAX_IDLE_TIME_MS: int = int(
        os.getenv("MONGO_MAX_IDLE_TIME_MS", "300000")
    ) 
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = int(
        os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "30000")
    )
    MONGO_CONNECT_TIMEOUT_MS: int = int(
        os.getenv("MONGO_CONNECT_TIMEOUT_MS", "20000")
    )
    MONGO_SOCKET_TIMEOUT_MS: int = int(
        os.getenv("MONGO_SOCKET_TIMEOUT_MS", "300000")
    )

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
        "MONGO_URI": Config.MONGO_URI,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    # Validate numeric ranges for database configuration
    if Config.MONGO_MAX_POOL_SIZE < Config.MONGO_MIN_POOL_SIZE:
        raise ValueError(
            f"MONGO_MAX_POOL_SIZE ({Config.MONGO_MAX_POOL_SIZE}) must be >= "
            f"MONGO_MIN_POOL_SIZE ({Config.MONGO_MIN_POOL_SIZE})"
        )
