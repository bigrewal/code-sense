import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..config import Config


@dataclass
class FileEntry:
    sha1: str
    language: str | None
    supported: bool


@dataclass
class RepoFileChanges:
    current_files: dict[str, FileEntry]
    new_files: set[str]
    changed_files: set[str]
    deleted_files: set[str]
    unchanged_files: set[str]


def _is_excluded(path: Path, repo_path: Path) -> bool:
    if path.suffix in {".sqlite", ".sqlite-shm", ".sqlite-wal"}:
        return True
    try:
        parts = path.relative_to(repo_path).parts
    except ValueError:
        return False
    return any(marker in parts for marker in Config.IGNORE_FOLDERS) or any(p.startswith(".") for p in parts)


def _sha1_bytes(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def build_repo_file_changes(repo_path: Path, previous_state: dict[str, dict]) -> RepoFileChanges:
    repo_path = Path(repo_path)
    current: dict[str, FileEntry] = {}
    supported_current_paths: set[str] = set()

    for path in repo_path.rglob("*"):
        if not path.is_file() or _is_excluded(path, repo_path):
            continue
        rel = str(path.relative_to(repo_path))
        language = Config.SUPPORTED_LANGUAGES.get(path.suffix.lower())
        current[rel] = FileEntry(
            sha1=_sha1_bytes(path),
            language=language,
            supported=bool(language),
        )
        if language:
            supported_current_paths.add(rel)

    previous_paths = set(previous_state.keys())
    deleted = previous_paths - supported_current_paths
    new = supported_current_paths - previous_paths

    existing_paths = supported_current_paths - new
    changed = {rel for rel in existing_paths if (previous_state.get(rel) or {}).get("sha1") != current[rel].sha1}
    unchanged = existing_paths - changed

    return RepoFileChanges(
        current_files=current,
        new_files=new,
        changed_files=changed,
        deleted_files=deleted,
        unchanged_files=unchanged,
    )
