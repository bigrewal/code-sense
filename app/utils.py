from datetime import datetime, timezone
from .config import Config
from pathlib import Path

def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_repo_dir(repo_name: str) -> str:
    return f"{Config.BASE_REPO_DIR}/{repo_name}"

def get_repo_path(repo_name: str) -> Path:
    return Path(Config.BASE_REPO_DIR) / repo_name