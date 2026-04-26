from datetime import datetime, timezone
from pathlib import Path

from .config import Config


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_repo_path(repo_name: str) -> Path:
    return Path(Config.BASE_REPO_DIR) / repo_name
