import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from .models.data_model import IngestionStage, IngestionStageStatus, IngestionJobStatus
from .utils import get_repo_path
from .chat_service import stream_chat, stateless_stream_chat
from .config import Config, validate_required_settings
from .db import (
    get_db_client,
    init_db_client,
)
from .repo_ingestion_pipeline import start_ingestion_pipeline
from .validators import (
    derive_repo_name_from_path,
    validate_repo_name,
    validate_repo_path,
    validate_conversation_id,
    validate_job_id,
)
from .timeouts import with_timeout
from .error_handlers import (
    http_exception_handler,
    validation_exception_handler,
    general_exception_handler,
)
from .middleware import RequestLoggingMiddleware

MAX_LIMIT = 200

logger = logging.getLogger(__name__)

app = FastAPI(title="Code Sense API", version="1.0.0")

_ingest_admission_lock = asyncio.Lock()
_ingest_execution_lock = asyncio.Lock()


def _cancel_active_jobs_on_lifecycle_event(reason: str) -> None:
    db_client = get_db_client()
    cancelled_count = db_client.cancel_active_ingestion_jobs(reason)
    if cancelled_count:
        logger.info("Marked %s active ingestion job(s) as cancelled", cancelled_count)

app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=3600,
)


@app.on_event("startup")
async def on_startup():
    logger.info("Initializing clients")
    validate_required_settings()
    init_db_client()
    _cancel_active_jobs_on_lifecycle_event("Ingestion cancelled: service restarted")


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Shutting down application - closing database connections")

    try:
        _cancel_active_jobs_on_lifecycle_event("Ingestion cancelled: service shutting down")
        db_client = get_db_client()
        db_client.close()
        logger.info("SQLite connection closed")
    except Exception as e:
        logger.error("Error closing SQLite connection: %s", str(e))

    logger.info("Shutdown complete")


class ChatRequest(BaseModel):
    conversation_id: str
    message: str

class StatelessChatRequest(BaseModel):
    repo_name: str
    message: str

class ErrorResponse(BaseModel):
    detail: str

class ConversationCreateRequest(BaseModel):
    repo_name: str


class ConversationCreateResponse(BaseModel):
    conversation_id: str
    repo_name: str
    created_at: datetime


class ConversationSummary(BaseModel):
    conversation_id: str
    repo_name: str
    created_at: datetime
    updated_at: datetime | None = None
    title: str | None = None


class MessageModel(BaseModel):
    role: str
    content: str
    message_type: str = "chat_message"
    stage: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ConversationMessagesResponse(BaseModel):
    conversation_id: str
    messages: list[MessageModel]


async def _db_call(operation_name: str, func, *args, **kwargs):
    return await with_timeout(
        asyncio.to_thread(func, *args, **kwargs),
        timeout_seconds=Config.DB_OPERATION_TIMEOUT,
        operation_name=operation_name,
    )

class IngestRequest(BaseModel):
    repo_name: str | None = None
    repo_path: str | None = None


class RepoBrowserRoot(BaseModel):
    name: str
    path: str


class RepoBrowserEntry(BaseModel):
    name: str
    path: str
    has_git: bool


class RepoBrowserResponse(BaseModel):
    path: str
    parent_path: str | None = None
    roots: list[RepoBrowserRoot]
    entries: list[RepoBrowserEntry]


def _paths_match(left: str, right: Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == right.resolve(strict=False)


def _path_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_repo_browser_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for raw_root in Config.REPO_BROWSER_ROOTS:
        try:
            root = Path(raw_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("Ignoring unavailable repo browser root: %s", raw_root)
            continue
        if not root.is_dir():
            continue
        key = str(root)
        if key not in seen:
            roots.append(root)
            seen.add(key)
    return roots


def _resolve_repo_browser_path(path: str | None, roots: list[Path]) -> Path:
    if not roots:
        raise HTTPException(status_code=500, detail="No valid repo browser roots configured")

    if not path:
        return roots[0]

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}") from exc

    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="path must be a directory")

    if not any(_path_relative_to(resolved, root) for root in roots):
        raise HTTPException(status_code=400, detail="path is outside configured repo browser roots")

    return resolved


def _repo_browser_parent_path(path: Path, roots: list[Path]) -> str | None:
    if any(path == root for root in roots):
        return None
    parent = path.parent
    if any(_path_relative_to(parent, root) for root in roots):
        return str(parent)
    return None


def _browse_repo_directory(path: str | None) -> RepoBrowserResponse:
    roots = _allowed_repo_browser_roots()
    current = _resolve_repo_browser_path(path, roots)
    entries: list[RepoBrowserEntry] = []

    try:
        children = sorted(current.iterdir(), key=lambda child: child.name.lower())
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"Unable to read directory: {current}") from exc

    for child in children:
        if len(entries) >= Config.REPO_BROWSER_MAX_ENTRIES:
            break
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
            resolved_child = child.resolve(strict=True)
        except OSError:
            continue
        entries.append(
            RepoBrowserEntry(
                name=child.name,
                path=str(resolved_child),
                has_git=(resolved_child / ".git").exists(),
            )
        )

    return RepoBrowserResponse(
        path=str(current),
        parent_path=_repo_browser_parent_path(current, roots),
        roots=[RepoBrowserRoot(name=root.name or str(root), path=str(root)) for root in roots],
        entries=entries,
    )


def _repo_name_for_ingest_path(db_client, repo_path: Path, requested_repo_name: str | None) -> str:
    repo_name = validate_repo_name(requested_repo_name) if requested_repo_name else derive_repo_name_from_path(repo_path)
    existing_path = db_client.get_repo_local_path(repo_name)

    if existing_path and not _paths_match(existing_path, repo_path):
        if requested_repo_name:
            raise HTTPException(
                status_code=409,
                detail=f"repo_name already exists for a different path: {repo_name}",
            )
        suffix = hashlib.sha1(str(repo_path).encode("utf-8")).hexdigest()[:8]
        repo_name = validate_repo_name(f"{repo_name[:246]}-{suffix}")

    return repo_name


async def _run_ingestion_job(**kwargs):
    """Ensure ingestion jobs execute one-at-a-time and cancellation is persisted."""
    job_id = kwargs["job_id"]
    db_client = get_db_client()

    async with _ingest_execution_lock:
        try:
            await start_ingestion_pipeline(**kwargs)
        except asyncio.CancelledError:
            await with_timeout(
                asyncio.to_thread(
                    db_client.cancel_active_ingestion_jobs,
                    f"Ingestion cancelled: job {job_id} interrupted",
                ),
                timeout_seconds=Config.DB_OPERATION_TIMEOUT,
                operation_name="Cancel interrupted ingestion job",
            )
            raise
    

@app.post("/conversations", response_model=ConversationCreateResponse)
async def create_conversation(req: ConversationCreateRequest):
    repo_name = validate_repo_name(req.repo_name)
    result = await _db_call("Create conversation", get_db_client().create_conversation, repo_name=repo_name)
    return ConversationCreateResponse(
        conversation_id=result["conversation_id"],
        repo_name=result["repo_name"],
        created_at=result["created_at"],
    )


@app.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    repo_name: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    if repo_name:
        repo_name = validate_repo_name(repo_name)

    docs = await _db_call(
        "List conversations",
        get_db_client().list_conversations,
        repo_name=repo_name,
        limit=limit,
        offset=offset,
    )

    return [
        ConversationSummary(
            conversation_id=str(d["_id"]),
            repo_name=d.get("repo_name"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            title=d.get("title"),
        )
        for d in docs
    ]


@app.get("/conversations/{conversation_id}/messages", response_model=ConversationMessagesResponse)
async def list_conversation_messages(conversation_id: str, limit: int = Query(200, ge=1, le=500)):
    conversation_id = validate_conversation_id(conversation_id)
    db_client = get_db_client()
    exists = await _db_call("Check conversation exists", db_client.conversation_exists, conversation_id)

    if not exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    docs = await _db_call(
        "List conversation messages",
        db_client.list_conversation_messages,
        conversation_id=conversation_id,
        limit=limit,
    )

    messages = [
        MessageModel(
            role=m["role"],
            content=m["content"],
            message_type=m.get("message_type", "chat_message"),
            stage=m.get("stage"),
            status=m.get("status"),
            metadata=m.get("metadata", {}),
            created_at=m.get("created_at"),
        )
        for m in docs
    ]

    return ConversationMessagesResponse(conversation_id=conversation_id, messages=messages)


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    conversation_id = validate_conversation_id(conversation_id)
    db_client = get_db_client()

    try:
        await _db_call("Delete conversation", db_client.delete_conversation, conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"message": "Conversation deleted"}


@app.post("/chat", responses={400: {"model": ErrorResponse}})
async def chat(req: ChatRequest):
    if not req.conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id is required")
    if not req.message:
        raise HTTPException(status_code=400, detail="message is required")

    return StreamingResponse(
        stream_chat(conversation_id=req.conversation_id, user_message=req.message),
        media_type="application/x-ndjson",
    )


@app.post("/stateless/chat", responses={400: {"model": ErrorResponse}})
async def stateless_chat(req: StatelessChatRequest):
    if not req.repo_name:
        raise HTTPException(status_code=400, detail="repo_name is required")
    if not req.message:
        raise HTTPException(status_code=400, detail="message is required")

    return StreamingResponse(
        stateless_stream_chat(repo_name=req.repo_name, user_message=req.message),
        media_type="application/x-ndjson",
    )


@app.get("/local/repos/browse", response_model=RepoBrowserResponse)
async def browse_local_repos(path: str | None = None):
    return await _db_call("Browse local repositories", _browse_repo_directory, path)


@app.post("/ingest", responses={404: {"model": ErrorResponse}})
async def ingest_repo(
    background_tasks: BackgroundTasks,
    ingest_request: IngestRequest = None,
):
    if not ingest_request or not (ingest_request.repo_name or ingest_request.repo_path):
        raise HTTPException(status_code=400, detail="Provide repo_path or repo_name")

    db_client = get_db_client()

    if ingest_request.repo_path:
        local_repo_path = validate_repo_path(ingest_request.repo_path)
        repo_name = _repo_name_for_ingest_path(db_client, local_repo_path, ingest_request.repo_name)
    else:
        repo_name = validate_repo_name(ingest_request.repo_name)
        local_repo_path = get_repo_path(repo_name)
        if not local_repo_path.exists():
            raise HTTPException(status_code=404, detail=f"Repository not found: {local_repo_path}")
        if not local_repo_path.is_dir():
            raise HTTPException(status_code=400, detail=f"Repository path is not a directory: {local_repo_path}")
        local_repo_path = local_repo_path.resolve()

    logger.info("Processing repo for ingestion: %s path=%s", repo_name, local_repo_path)

    async with _ingest_admission_lock:
        active_job = await _db_call("Check active ingestion job", db_client.get_active_ingestion_job)
        if active_job:
            active_job_id = active_job.get("job_id", "unknown")
            raise HTTPException(
                status_code=409,
                detail=f"An ingestion job is already in progress ({active_job_id}).",
            )

        job_id = str(uuid4())

        job = IngestionJobStatus(
            job_id=job_id,
            repo_name=repo_name,
            status="queued",
            current_stage=IngestionStage.PRECHECK,
            stage_status={
                IngestionStage.PRECHECK: IngestionStageStatus.PENDING,
                IngestionStage.MENTAL_MODEL: IngestionStageStatus.PENDING,
            },
        )

        await _db_call(
            "Create ingestion job",
            db_client.upsert_ingestion_job,
            job,
            extra_fields={"operation": "full_ingest"},
        )

        background_tasks.add_task(
            _run_ingestion_job,
            local_repo_path=local_repo_path,
            repo_name=repo_name,
            job_id=job_id,
        )

    return {
        "job_id": job_id,
        "repo_name": repo_name,
        "status": "queued",
    }

@app.delete("/repos", responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def delete_repo(repo_name: str, delete_files: bool = False):
    """Delete a code repository and its associated data, including ingestion jobs."""
    repo_name = validate_repo_name(repo_name)
    db_client = get_db_client()
    stored_repo_path = await _db_call("Get repo path", db_client.get_repo_local_path, repo_name)
    local_repo_path = Path(stored_repo_path).expanduser() if stored_repo_path else get_repo_path(repo_name)

    base_dir = Path(Config.BASE_REPO_DIR).resolve()
    repo_path = Path(local_repo_path).resolve(strict=False)

    if delete_files:
        try:
            repo_path.relative_to(base_dir)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="delete_files is only supported for managed repos under BASE_REPO_DIR",
            ) from exc

        if local_repo_path.exists():
            try:
                import shutil
                shutil.rmtree(local_repo_path)
            except Exception as exc:
                logger.exception("Failed to delete repo files", exc_info=exc)
                raise HTTPException(status_code=500, detail="Failed to delete repo files")
        else:
            logger.info("Repo files not found for %s, continuing with DB cleanup", repo_name)

    try:
        deletion_result = await _db_call("Delete repo data", db_client.delete_repo_data, repo_name)
    except Exception as exc:
        logger.exception("Failed to delete repo documents", exc_info=exc)
        raise HTTPException(status_code=500, detail="Failed to delete repo data")

    return {
        "message": f"Repository {repo_name} and its data have been deleted.",
        "total_deleted": deletion_result.get("total_deleted", 0),
    }

@app.get("/status")
async def get_status(
    job_id: str | None = None,
    status: str | None = None,
    repo_name: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    skip: int = Query(0, ge=0),
):
    if job_id:
        job_id = validate_job_id(job_id)
    if repo_name:
        repo_name = validate_repo_name(repo_name)

    db_client = get_db_client()

    if job_id:
        job = await _db_call("Get job status", db_client.get_job_status, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    jobs, total = await _db_call(
        "List jobs",
        db_client.list_jobs,
        status=status,
        repo_name=repo_name,
        limit=limit,
        skip=skip,
        include_total=True,
    )
    return {
        "jobs": jobs,
        "count": len(jobs),
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@app.get("/repos")
async def list_repos():
    ingested_repos = await _db_call("List repos", get_db_client().list_ingested_repos)
    return {"repos": ingested_repos}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    job_id = validate_job_id(job_id)
    db_client = get_db_client()

    job = await _db_call("Get job", db_client.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail="Job is running. Retry delete after it finishes.",
        )

    ok = await _db_call("Delete job", db_client.delete_job, job_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete job")

    return {"job_id": job_id, "deleted": True}


@app.get("/health")
async def health():
    from fastapi.responses import JSONResponse

    overall_status = "healthy"
    components = {}

    try:
        db_health = get_db_client().health_check()
        components["sqlite"] = db_health
        if db_health["status"] != "healthy":
            overall_status = "unhealthy"
    except Exception as e:
        components["sqlite"] = {"status": "unhealthy", "error": str(e)}
        overall_status = "unhealthy"

    response = {
        "status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }

    status_code = 200 if overall_status == "healthy" else 503
    return JSONResponse(content=response, status_code=status_code)
