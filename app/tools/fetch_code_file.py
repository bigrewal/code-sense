import logging
from pathlib import Path

from ..config import Config
from ..db import get_db_client

logger = logging.getLogger(__name__)


def fetch_code_file(repo_name: str, file_path: str) -> str:
    if not file_path or not isinstance(file_path, str):
        raise ValueError("Missing or invalid file_path parameter")
    if Path(file_path).is_absolute():
        raise ValueError("file_path must be repo-relative")

    registered_path = get_db_client().get_repo_local_path(repo_name)
    repo_root = Path(registered_path or Path(Config.BASE_REPO_DIR) / repo_name).expanduser().resolve(strict=False)
    full_path = (repo_root / file_path).resolve()

    try:
        full_path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("file_path escapes repository root") from exc

    try:
        if not full_path.is_file():
            raise FileNotFoundError(full_path)
        return full_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.exception("Failed to fetch code file: repo=%s path=%s", repo_name, file_path)
        raise RuntimeError(f"Failed to read file: {e}") from e
