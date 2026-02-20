import re
from fastapi import HTTPException

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9_/-]+$")
_OBJECT_ID_RE = re.compile(r"^[a-f0-9]{24}$")
_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


def _require_non_empty_str(value: str, field_name: str) -> str:
    if not value or not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field_name} must be non-empty string")
    return value


def validate_repo_name(repo_name: str) -> str:
    repo_name = _require_non_empty_str(repo_name, "repo_name")
    if ".." in repo_name or repo_name.startswith("/"):
        raise HTTPException(status_code=400, detail="repo_name contains invalid path sequences")
    if len(repo_name) > 255:
        raise HTTPException(status_code=400, detail="repo_name too long (max 255 characters)")
    if not _REPO_NAME_RE.fullmatch(repo_name):
        raise HTTPException(
            status_code=400,
            detail="repo_name contains invalid characters (allowed: a-z, A-Z, 0-9, -, _, /)",
        )
    return repo_name


def validate_conversation_id(conversation_id: str) -> str:
    conversation_id = _require_non_empty_str(conversation_id, "conversation_id")
    if not _OBJECT_ID_RE.fullmatch(conversation_id):
        raise HTTPException(status_code=400, detail="Invalid conversation_id format (must be 24-char hex)")
    return conversation_id


def validate_job_id(job_id: str) -> str:
    job_id = _require_non_empty_str(job_id, "job_id")
    if not _UUID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id format (must be UUID)")
    return job_id
