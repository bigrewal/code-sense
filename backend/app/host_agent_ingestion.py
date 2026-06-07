from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from .db import create_sqlite_client
from .models.data_model import IngestionJobStatus, IngestionStage, IngestionStageStatus
from .repo_ingestion_pipeline.file_state import build_repo_file_changes
from .repo_ingestion_pipeline.mental_model_gen import MENTAL_MODEL_TYPES
from .validators import derive_repo_name_from_path, validate_repo_name


HOST_AGENT_OPERATION = "host_agent_ingest"
DEFAULT_BATCH_LIMIT = 8
DEFAULT_MAX_CONTENT_BYTES = 40_000
PROJECT_DB_RELATIVE_PATH = Path(".codesense") / "code_sense.sqlite3"

_JOB_DB_PATHS: dict[str, str] = {}
_REPO_DB_PATHS: dict[str, str] = {}


def _payload(status: IngestionStageStatus, **extra: Any) -> dict[str, Any]:
    return {"status": status.value, **extra}


def _save_job(db_client, job_id: str, repo_name: str, status: str, stage: IngestionStage, stages: dict, **kwargs):
    db_client.upsert_ingestion_job(
        IngestionJobStatus(
            job_id=job_id,
            repo_name=repo_name,
            status=status,
            current_stage=stage,
            stage_status=stages,
        ),
        **kwargs,
    )


def _resolve_repo_path(repo_path: str) -> Path:
    if not repo_path or not isinstance(repo_path, str):
        raise ValueError("repo_path must be a non-empty string")
    try:
        resolved = Path(repo_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Repository path not found: {repo_path}") from exc
    if not resolved.is_dir():
        raise ValueError(f"repo_path must be an existing directory: {repo_path}")
    return resolved


def _project_db_path(repo_path: Path) -> Path:
    return repo_path / PROJECT_DB_RELATIVE_PATH


def _connect_db(db_path: str):
    return create_sqlite_client(db_path)


def _connect_project_db(repo_path: Path):
    return _connect_db(str(_project_db_path(repo_path)))


def _resolve_explicit_db_path(db_path: str) -> str:
    if not db_path or not isinstance(db_path, str):
        raise ValueError("db_path must be a non-empty string")
    return str(Path(db_path).expanduser().resolve(strict=False))


def _remember_db_path(job_id: str | None, repo_name: str | None, db_path: str) -> None:
    if job_id:
        _JOB_DB_PATHS[job_id] = db_path
    if repo_name:
        _REPO_DB_PATHS[repo_name] = db_path


def _db_path_for_job(job_id: str, repo_path: str | None = None, db_path: str | None = None) -> str:
    if db_path:
        return _resolve_explicit_db_path(db_path)
    if job_id in _JOB_DB_PATHS:
        return _JOB_DB_PATHS[job_id]
    if repo_path:
        return str(_project_db_path(_resolve_repo_path(repo_path)))
    raise ValueError("Pass repo_path or db_path so Code-Sense can find this project's local database")


def _db_path_for_repo(repo_name: str, repo_path: str | None = None, db_path: str | None = None) -> str:
    if db_path:
        return _resolve_explicit_db_path(db_path)
    if repo_path:
        return str(_project_db_path(_resolve_repo_path(repo_path)))
    if repo_name in _REPO_DB_PATHS:
        return _REPO_DB_PATHS[repo_name]
    raise ValueError("Pass repo_path or db_path so Code-Sense can find this project's local database")


def _paths_match(left: str, right: Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == right.resolve(strict=False)


def _repo_name_for_path(db_client, repo_path: Path, requested_repo_name: str | None) -> str:
    repo_name = validate_repo_name(requested_repo_name) if requested_repo_name else derive_repo_name_from_path(repo_path)
    existing_path = db_client.get_repo_local_path(repo_name)
    if existing_path and not _paths_match(existing_path, repo_path):
        if requested_repo_name:
            raise ValueError(f"repo_name already exists for a different path: {repo_name}")
        suffix = hashlib.sha1(str(repo_path).encode("utf-8")).hexdigest()[:8]
        repo_name = validate_repo_name(f"{repo_name[:246]}-{suffix}")
    return repo_name


def _estimate_token_count(path: Path) -> int:
    try:
        return max(1, path.stat().st_size // 4)
    except OSError:
        return 0


def _delete_removed_artifacts(db_client, repo_name: str, deleted_files: set[str]) -> int:
    deleted_state = db_client.delete_repo_file_states(repo_name, sorted(deleted_files))
    deleted_docs = db_client.delete_mental_model_documents(
        repo_name=repo_name,
        file_paths=sorted(deleted_files),
        document_types=[MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]],
    )
    return deleted_state + deleted_docs


def _pending_files(db_client, repo_name: str, repo_path: Path) -> list[dict[str, Any]]:
    file_states = db_client.get_repo_file_states(repo_name)
    docs = db_client.list_mental_model_documents(
        repo_name=repo_name,
        document_types=[MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]],
    )
    current_doc_shas = {
        doc["file_path"]: doc.get("sha1")
        for doc in docs
        if doc.get("file_path") and doc.get("sha1")
    }

    pending: list[dict[str, Any]] = []
    for file_path, state in sorted(file_states.items()):
        if not state.get("supported"):
            continue
        if current_doc_shas.get(file_path) == state.get("sha1"):
            continue
        absolute_path = repo_path / file_path
        pending.append(
            {
                "file_path": file_path,
                "absolute_path": str(absolute_path),
                "sha1": state.get("sha1"),
                "language": state.get("language"),
                "size_bytes": absolute_path.stat().st_size if absolute_path.exists() else 0,
            }
        )
    return pending


def _job_repo_path(db_client, job: dict[str, Any]) -> Path:
    stages = job.get("stages") or {}
    precheck_metrics = (stages.get(IngestionStage.PRECHECK.value) or {}).get("metrics") or {}
    local_repo_path = precheck_metrics.get("local_repo_path") or db_client.get_repo_local_path(job["repo_name"])
    if not local_repo_path:
        raise ValueError(f"No local repo path recorded for job {job['job_id']}")
    return _resolve_repo_path(local_repo_path)


def _get_host_job(db_client, job_id: str) -> dict[str, Any]:
    job = db_client.get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    if job.get("operation") != HOST_AGENT_OPERATION:
        raise ValueError(f"Job is not a host-agent ingestion job: {job_id}")
    return job


def _replace_file_document(db_client, repo_name: str, file_path: str, doc_type: str, data: str, sha1: str) -> None:
    db_client.delete_mental_model_documents(
        repo_name=repo_name,
        file_paths=[file_path],
        document_types=[MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]],
    )
    db_client.upsert_mental_model_document(
        repo_name=repo_name,
        file_path=file_path,
        document_type=doc_type,
        data=data,
        sha1=sha1,
    )


def _classification_for_result(result: dict[str, Any]) -> tuple[str, str]:
    raw_summary = str(result.get("summary") or "").strip()
    raw_classification = str(result.get("classification") or "").strip().lower()
    if raw_summary.upper() == "IGNORE" or raw_classification in {
        "ignore",
        "ignored",
        "not_critical",
        "not-critical",
        "non_critical",
        "non-critical",
    }:
        return MENTAL_MODEL_TYPES["IGNORED"], "IGNORE"
    if not raw_summary:
        raise ValueError("Each critical result must include a non-empty summary")
    return MENTAL_MODEL_TYPES["BRIEF"], raw_summary


def _context_token_estimate(text: str) -> int:
    return max(0, len(text) // 4)


def _serialize_file_changes(file_changes) -> dict[str, Any]:
    return {
        "new_files": sorted(file_changes.new_files),
        "changed_files": sorted(file_changes.changed_files),
        "deleted_files": sorted(file_changes.deleted_files),
        "unchanged_files": sorted(file_changes.unchanged_files),
        "current_file_count": len(file_changes.current_files),
    }


def start_host_agent_ingestion(repo_path: str, repo_name: str | None = None) -> dict[str, Any]:
    local_repo_path = _resolve_repo_path(repo_path)
    db_client = _connect_project_db(local_repo_path)
    db_path = str(_project_db_path(local_repo_path))

    try:
        resolved_repo_name = _repo_name_for_path(db_client, local_repo_path, repo_name)

        job_id = str(uuid4())
        previous_state = db_client.get_repo_file_states(resolved_repo_name)
        file_changes = build_repo_file_changes(local_repo_path, previous_state)

        state_rows = [
            {
                "file_path": rel,
                "sha1": entry.sha1,
                "language": entry.language,
                "supported": entry.supported,
                "token_count": _estimate_token_count(local_repo_path / rel),
            }
            for rel, entry in sorted(file_changes.current_files.items())
            if entry.supported
        ]
        db_client.upsert_repo_file_states(resolved_repo_name, state_rows)
        removed_count = _delete_removed_artifacts(db_client, resolved_repo_name, file_changes.deleted_files)

        pending = _pending_files(db_client, resolved_repo_name, local_repo_path)
        precheck_metrics = {
            "local_repo_path": str(local_repo_path),
            "db_path": db_path,
            "supported_file_count": len(state_rows),
            "unsupported_file_count": len(file_changes.current_files) - len(state_rows),
            "new_files": len(file_changes.new_files),
            "changed_files": len(file_changes.changed_files),
            "deleted_files": len(file_changes.deleted_files),
            "unchanged_files": len(file_changes.unchanged_files),
            "removed_artifacts": removed_count,
        }

        _save_job(
            db_client,
            job_id,
            resolved_repo_name,
            "running",
            IngestionStage.MENTAL_MODEL,
            {
                IngestionStage.PRECHECK: _payload(IngestionStageStatus.COMPLETED, metrics=precheck_metrics),
                IngestionStage.MENTAL_MODEL: _payload(
                    IngestionStageStatus.RUNNING,
                    metrics={"pending_files": len(pending), "processed_files": 0},
                ),
            },
            extra_fields={"operation": HOST_AGENT_OPERATION},
        )
        _remember_db_path(job_id, resolved_repo_name, db_path)

        return {
            "job_id": job_id,
            "repo_name": resolved_repo_name,
            "repo_path": str(local_repo_path),
            "db_path": db_path,
            "status": "running",
            "operation": HOST_AGENT_OPERATION,
            "pending_files": len(pending),
            "file_changes": _serialize_file_changes(file_changes),
        }
    finally:
        db_client.close()


def get_next_file_batch(
    job_id: str,
    *,
    repo_path: str | None = None,
    db_path: str | None = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    include_content: bool = False,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> dict[str, Any]:
    resolved_db_path = _db_path_for_job(job_id, repo_path=repo_path, db_path=db_path)
    db_client = _connect_db(resolved_db_path)

    try:
        job = _get_host_job(db_client, job_id)
        local_repo_path = _job_repo_path(db_client, job)
        _remember_db_path(job_id, job["repo_name"], resolved_db_path)
        limit = max(1, min(int(limit), 50))
        max_content_bytes = max(0, int(max_content_bytes))

        pending = _pending_files(db_client, job["repo_name"], local_repo_path)
        files = pending[:limit]
        for item in files:
            if not include_content:
                continue
            path = Path(item["absolute_path"])
            data = path.read_bytes()[:max_content_bytes]
            item["content"] = data.decode("utf-8", errors="replace")
            item["content_truncated"] = path.stat().st_size > len(data)

        return {
            "job_id": job_id,
            "repo_name": job["repo_name"],
            "repo_path": str(local_repo_path),
            "db_path": resolved_db_path,
            "pending_files": len(pending),
            "returned_files": len(files),
            "files": files,
            "instructions": (
                "For each file, read the full source when needed. Return either summary='IGNORE' "
                "for non-critical files, or a 100-200 word summary in the Code-Sense file-brief format."
            ),
        }
    finally:
        db_client.close()


def save_file_briefs(
    job_id: str,
    file_results: list[dict[str, Any]],
    *,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    if not file_results:
        raise ValueError("file_results must contain at least one result")

    resolved_db_path = _db_path_for_job(job_id, repo_path=repo_path, db_path=db_path)
    db_client = _connect_db(resolved_db_path)

    try:
        job = _get_host_job(db_client, job_id)
        local_repo_path = _job_repo_path(db_client, job)
        _remember_db_path(job_id, job["repo_name"], resolved_db_path)
        file_states = db_client.get_repo_file_states(job["repo_name"])

        saved = 0
        for result in file_results:
            file_path = str(result.get("file_path") or "").strip()
            if not file_path:
                raise ValueError("Each result must include file_path")
            state = file_states.get(file_path)
            if not state:
                raise ValueError(f"Unknown or unsupported file_path for repo {job['repo_name']}: {file_path}")
            if not (local_repo_path / file_path).exists():
                raise ValueError(f"File no longer exists: {file_path}")
            doc_type, data = _classification_for_result(result)
            _replace_file_document(db_client, job["repo_name"], file_path, doc_type, data, state["sha1"])
            saved += 1

        pending = _pending_files(db_client, job["repo_name"], local_repo_path)
        critical_count = db_client.count_mental_model_documents(
            repo_name=job["repo_name"],
            document_type=MENTAL_MODEL_TYPES["BRIEF"],
        )
        ignored_count = db_client.count_mental_model_documents(
            repo_name=job["repo_name"],
            document_type=MENTAL_MODEL_TYPES["IGNORED"],
        )

        _save_job(
            db_client,
            job_id,
            job["repo_name"],
            "running",
            IngestionStage.MENTAL_MODEL,
            {
                IngestionStage.MENTAL_MODEL: _payload(
                    IngestionStageStatus.RUNNING,
                    metrics={
                        "pending_files": len(pending),
                        "processed_files": critical_count + ignored_count,
                        "critical_files": critical_count,
                        "files_ignored": ignored_count,
                    },
                )
            },
        )

        return {
            "job_id": job_id,
            "repo_name": job["repo_name"],
            "db_path": resolved_db_path,
            "saved": saved,
            "pending_files": len(pending),
            "critical_files": critical_count,
            "files_ignored": ignored_count,
        }
    finally:
        db_client.close()


def build_repo_context(
    job_id: str,
    *,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    resolved_db_path = _db_path_for_job(job_id, repo_path=repo_path, db_path=db_path)
    db_client = _connect_db(resolved_db_path)

    try:
        job = _get_host_job(db_client, job_id)
        local_repo_path = _job_repo_path(db_client, job)
        _remember_db_path(job_id, job["repo_name"], resolved_db_path)
        pending = _pending_files(db_client, job["repo_name"], local_repo_path)
        if pending:
            raise ValueError(f"Cannot build repo context while {len(pending)} file(s) are still pending")

        briefs = db_client.list_mental_model_documents(
            repo_name=job["repo_name"],
            document_types=[MENTAL_MODEL_TYPES["BRIEF"]],
        )
        context = "\n\n".join(doc["data"] for doc in briefs if doc.get("data"))
        db_client.upsert_repo_context(job["repo_name"], context)
        db_client.add_ingested_repo(job["repo_name"], job_id, local_path=str(local_repo_path))

        critical_count = db_client.count_mental_model_documents(
            repo_name=job["repo_name"],
            document_type=MENTAL_MODEL_TYPES["BRIEF"],
        )
        ignored_count = db_client.count_mental_model_documents(
            repo_name=job["repo_name"],
            document_type=MENTAL_MODEL_TYPES["IGNORED"],
        )
        metrics = {
            "critical_files": critical_count,
            "files_ignored": ignored_count,
            "repo_context_token_count": _context_token_estimate(context),
        }
        _save_job(
            db_client,
            job_id,
            job["repo_name"],
            "completed",
            IngestionStage.MENTAL_MODEL,
            {IngestionStage.MENTAL_MODEL: _payload(IngestionStageStatus.COMPLETED, metrics=metrics)},
        )

        try:
            from .chat_service import invalidate_repo_context_cache

            invalidate_repo_context_cache(job["repo_name"])
        except Exception:
            pass

        return {
            "job_id": job_id,
            "repo_name": job["repo_name"],
            "db_path": resolved_db_path,
            "status": "completed",
            **metrics,
        }
    finally:
        db_client.close()


def get_repo_context(
    repo_name: str,
    *,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    repo_name = validate_repo_name(repo_name)
    resolved_db_path = _db_path_for_repo(repo_name, repo_path=repo_path, db_path=db_path)
    db_client = _connect_db(resolved_db_path)

    try:
        _remember_db_path(None, repo_name, resolved_db_path)
        context = db_client.get_repo_context(repo_name)
        if not context:
            raise ValueError(f"No repo context found for repo: {repo_name}")
        return {
            "repo_name": repo_name,
            "db_path": resolved_db_path,
            "context": context,
            "estimated_tokens": _context_token_estimate(context),
        }
    finally:
        db_client.close()


def get_file_brief(
    repo_name: str,
    file_path: str,
    *,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    repo_name = validate_repo_name(repo_name)
    if not file_path or not isinstance(file_path, str):
        raise ValueError("file_path must be a non-empty string")
    resolved_db_path = _db_path_for_repo(repo_name, repo_path=repo_path, db_path=db_path)
    db_client = _connect_db(resolved_db_path)

    try:
        _remember_db_path(None, repo_name, resolved_db_path)
        brief = db_client.get_brief_file_overview(repo_name, file_path)
        if not brief:
            raise ValueError(f"No brief found for {file_path} in repo {repo_name}")
        return {"repo_name": repo_name, "file_path": file_path, "db_path": resolved_db_path, "brief": brief}
    finally:
        db_client.close()
