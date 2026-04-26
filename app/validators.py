import re
from pathlib import Path

from fastapi import HTTPException

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9_/-]+$")
_REPO_NAME_INVALID_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_OBJECT_ID_RE = re.compile(r"^[a-f0-9]{24}$")
_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


def _require_non_empty_str(value: str, field_name: str) -> str:
    if not value or not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field_name} must be non-empty string")
    return value


def _validate_regex(value: str, regex: re.Pattern, detail: str) -> str:
    if not regex.fullmatch(value):
        raise HTTPException(status_code=400, detail=detail)
    return value


def validate_repo_name(repo_name: str) -> str:
    repo_name = _require_non_empty_str(repo_name, "repo_name")
    if ".." in repo_name or repo_name.startswith("/"):
        raise HTTPException(status_code=400, detail="repo_name contains invalid path sequences")
    if len(repo_name) > 255:
        raise HTTPException(status_code=400, detail="repo_name too long (max 255 characters)")
    return _validate_regex(
        repo_name,
        _REPO_NAME_RE,
        "repo_name contains invalid characters (allowed: a-z, A-Z, 0-9, -, _, /)",
    )


def derive_repo_name_from_path(repo_path: Path) -> str:
    raw_name = repo_path.name.strip()
    repo_name = _REPO_NAME_INVALID_CHARS_RE.sub("-", raw_name).strip("-_")
    if not repo_name:
        raise HTTPException(status_code=400, detail="Unable to derive repo_name from repo_path")
    return validate_repo_name(repo_name[:255])


def validate_repo_path(repo_path: str) -> Path:
    repo_path = _require_non_empty_str(repo_path, "repo_path")
    try:
        resolved = Path(repo_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=f"Repository path not found: {repo_path}") from exc

    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="repo_path must be an existing directory")

    return resolved


def validate_conversation_id(conversation_id: str) -> str:
    return _validate_regex(
        _require_non_empty_str(conversation_id, "conversation_id"),
        _OBJECT_ID_RE,
        "Invalid conversation_id format (must be 24-char hex)",
    )


def validate_job_id(job_id: str) -> str:
    return _validate_regex(
        _require_non_empty_str(job_id, "job_id"),
        _UUID_RE,
        "Invalid job_id format (must be UUID)",
    )
