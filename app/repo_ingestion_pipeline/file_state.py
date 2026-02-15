import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Set

from ..config import Config


@dataclass
class FileEntry:
    sha1: str
    language: Optional[str]
    supported: bool


@dataclass
class RepoFileChanges:
    current_files: Dict[str, FileEntry]
    new_files: Set[str]
    changed_files: Set[str]
    deleted_files: Set[str]
    unchanged_files: Set[str]


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


def build_repo_file_changes(repo_path: Path, previous_state: Dict[str, dict]) -> RepoFileChanges:
    repo_path = Path(repo_path)
    current: Dict[str, FileEntry] = {}
    current_paths: Set[str] = set()

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
        current_paths.add(rel)

    previous_paths = set(previous_state.keys())
    deleted = previous_paths - current_paths
    new = current_paths - previous_paths

    changed: Set[str] = set()
    unchanged: Set[str] = set()
    for rel in current_paths - new:
        prev_sha = (previous_state.get(rel) or {}).get("sha1")
        if prev_sha != current[rel].sha1:
            changed.add(rel)
        else:
            unchanged.add(rel)

    return RepoFileChanges(
        current_files=current,
        new_files=new,
        changed_files=changed,
        deleted_files=deleted,
        unchanged_files=unchanged,
    )
