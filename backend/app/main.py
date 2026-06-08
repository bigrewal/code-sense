import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field, model_validator

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

API_PREFIX = "/v1"
MAX_LIMIT = 200

logger = logging.getLogger(__name__)


_ingest_admission_lock = asyncio.Lock()
_ingest_execution_lock = asyncio.Lock()


def _cancel_active_jobs_on_lifecycle_event(reason: str) -> None:
    db_client = get_db_client()
    cancelled_count = db_client.cancel_active_ingestion_jobs(reason)
    if cancelled_count:
        logger.info("Marked %s active ingestion job(s) as cancelled", cancelled_count)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing clients")
    validate_required_settings()
    init_db_client()
    _cancel_active_jobs_on_lifecycle_event("Ingestion cancelled: service restarted")
    try:
        yield
    finally:
        logger.info("Shutting down application - closing database connections")
        try:
            _cancel_active_jobs_on_lifecycle_event("Ingestion cancelled: service shutting down")
            db_client = get_db_client()
            db_client.close()
            logger.info("SQLite connection closed")
        except Exception as exc:
            logger.error("Error closing SQLite connection: %s", exc)
        logger.info("Shutdown complete")


app = FastAPI(title="Code Sense API", version="1.0.0", lifespan=lifespan)

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


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    subdir_context_paths: list[str] = Field(default_factory=list)


class StatelessChatRequest(BaseModel):
    repo_name: str = Field(min_length=1)
    message: str = Field(min_length=1)
    subdir_context_paths: list[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str


class RepoSubdirOption(BaseModel):
    path: str
    file_count: int


class RepoSubdirOptionsResponse(BaseModel):
    repo_name: str
    subdirs: list[RepoSubdirOption]


class ConversationCreateRequest(BaseModel):
    repo_name: str = Field(min_length=1)


class ConversationCreateResponse(BaseModel):
    conversation_id: str
    repo_name: str
    created_at: datetime


class ConversationSummary(BaseModel):
    conversation_id: str
    repo_name: str | None = None
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


class IngestRequest(BaseModel):
    repo_name: str | None = None
    repo_path: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "IngestRequest":
        if not (self.repo_name or self.repo_path):
            raise ValueError("Provide repo_name or repo_path")
        return self


class IngestResponse(BaseModel):
    job_id: str
    repo_name: str
    status: str


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _db_call(operation_name: str, func, *args, **kwargs):
    return await with_timeout(
        asyncio.to_thread(func, *args, **kwargs),
        timeout_seconds=Config.DB_OPERATION_TIMEOUT,
        operation_name=operation_name,
    )


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


def _enforce_ingest_path_under_browser_roots(repo_path: Path) -> None:
    """Ingestion via repo_path must target a directory inside REPO_BROWSER_ROOTS.

    The repo browser already enforces this for UI-driven flows; the API surface
    needs the same boundary or it becomes a free-form path-traversal vector.
    """
    roots = _allowed_repo_browser_roots()
    if not roots:
        raise HTTPException(status_code=500, detail="No valid repo browser roots configured")
    if not any(_path_relative_to(repo_path, root) for root in roots):
        raise HTTPException(
            status_code=400,
            detail="repo_path is outside configured repo browser roots",
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


# ---------------------------------------------------------------------------
# v1 routers
# ---------------------------------------------------------------------------

api = APIRouter(prefix=API_PREFIX)
conversations_router = APIRouter(prefix="/conversations", tags=["conversations"])
chat_router = APIRouter(tags=["chat"])
repos_router = APIRouter(prefix="/repos", tags=["repositories"])
ingest_router = APIRouter(tags=["ingestion"])
jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])
local_router = APIRouter(prefix="/local", tags=["local"])


# ---------------------------- conversations --------------------------------

@conversations_router.post(
    "",
    response_model=ConversationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(req: ConversationCreateRequest):
    repo_name = validate_repo_name(req.repo_name)
    result = await _db_call("Create conversation", get_db_client().create_conversation, repo_name=repo_name)
    return ConversationCreateResponse(
        conversation_id=result["conversation_id"],
        repo_name=result["repo_name"],
        created_at=result["created_at"],
    )


@conversations_router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    repo_name: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
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


@conversations_router.get(
    "/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
    responses={404: {"model": ErrorResponse}},
)
async def list_conversation_messages(conversation_id: str, limit: int = Query(MAX_LIMIT, ge=1, le=500)):
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


@conversations_router.delete(
    "/{conversation_id}",
    responses={404: {"model": ErrorResponse}},
)
async def delete_conversation(conversation_id: str):
    conversation_id = validate_conversation_id(conversation_id)
    db_client = get_db_client()

    try:
        await _db_call("Delete conversation", db_client.delete_conversation, conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"message": "Conversation deleted"}


# -------------------------------- chat -------------------------------------

@chat_router.post(
    "/chat",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def chat(req: ChatRequest):
    conversation_id = validate_conversation_id(req.conversation_id)
    db_client = get_db_client()
    exists = await _db_call("Check conversation exists", db_client.conversation_exists, conversation_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return StreamingResponse(
        stream_chat(
            conversation_id=conversation_id,
            user_message=req.message,
            subdir_context_paths=req.subdir_context_paths,
        ),
        media_type="application/x-ndjson",
    )


@chat_router.post(
    "/stateless/chat",
    responses={400: {"model": ErrorResponse}},
)
async def stateless_chat(req: StatelessChatRequest):
    repo_name = validate_repo_name(req.repo_name)
    return StreamingResponse(
        stateless_stream_chat(
            repo_name=repo_name,
            user_message=req.message,
            subdir_context_paths=req.subdir_context_paths,
        ),
        media_type="application/x-ndjson",
    )


# ------------------------------- local -------------------------------------

@local_router.get("/repos/browse", response_model=RepoBrowserResponse)
async def browse_local_repos(path: str | None = None):
    return await _db_call("Browse local repositories", _browse_repo_directory, path)


# ------------------------------ ingest -------------------------------------

@ingest_router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def ingest_repo(
    background_tasks: BackgroundTasks,
    ingest_request: IngestRequest,
):
    db_client = get_db_client()

    if ingest_request.repo_path:
        local_repo_path = validate_repo_path(ingest_request.repo_path)
        _enforce_ingest_path_under_browser_roots(local_repo_path)
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

    return IngestResponse(job_id=job_id, repo_name=repo_name, status="queued")


# ------------------------------- repos -------------------------------------

@repos_router.get("")
async def list_repos():
    ingested_repos = await _db_call("List repos", get_db_client().list_ingested_repos)
    return {"repos": ingested_repos}


async def _list_repo_subdirs_response(repo_name: str):
    repo_name = validate_repo_name(repo_name)
    subdirs = await _db_call(
        "List repo subdirs",
        get_db_client().list_brief_subdir_options,
        repo_name,
    )
    return {"repo_name": repo_name, "subdirs": subdirs}


@repos_router.get("/subdirs", response_model=RepoSubdirOptionsResponse)
async def list_repo_subdirs_by_query(repo_name: str = Query(..., min_length=1)):
    return await _list_repo_subdirs_response(repo_name)


@repos_router.get("/{repo_name}/subdirs", response_model=RepoSubdirOptionsResponse)
async def list_repo_subdirs(repo_name: str):
    return await _list_repo_subdirs_response(repo_name)


@repos_router.delete(
    "/{repo_name}",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
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
                await asyncio.to_thread(shutil.rmtree, local_repo_path)
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Failed to delete repo files", exc_info=exc)
                raise HTTPException(status_code=500, detail="Failed to delete repo files")
        else:
            logger.info("Repo files not found for %s, continuing with DB cleanup", repo_name)

    try:
        deletion_result = await _db_call("Delete repo data", db_client.delete_repo_data, repo_name)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to delete repo documents", exc_info=exc)
        raise HTTPException(status_code=500, detail="Failed to delete repo data")

    return {
        "message": f"Repository {repo_name} and its data have been deleted.",
        "total_deleted": deletion_result.get("total_deleted", 0),
    }


# ------------------------------- jobs --------------------------------------

@jobs_router.get("")
async def list_jobs(
    status: str | None = None,
    repo_name: str | None = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    skip: int = Query(0, ge=0),
):
    if repo_name:
        repo_name = validate_repo_name(repo_name)

    db_client = get_db_client()
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


@jobs_router.get("/{job_id}", responses={404: {"model": ErrorResponse}})
async def get_job(job_id: str):
    job_id = validate_job_id(job_id)
    db_client = get_db_client()
    job = await _db_call("Get job status", db_client.get_job_status, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@jobs_router.delete(
    "/{job_id}",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
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


# Mount sub-routers under /v1, then mount /v1 on the app.
api.include_router(conversations_router)
api.include_router(chat_router)
api.include_router(local_router)
api.include_router(ingest_router)
api.include_router(repos_router)
api.include_router(jobs_router)
app.include_router(api)


# ---------------------------------------------------------------------------
# Unversioned health probe (k8s-style)
# ---------------------------------------------------------------------------


@app.get("/health", tags=["health"])
async def health():
    overall_status = "healthy"
    components: dict[str, Any] = {}

    try:
        db_health = get_db_client().health_check()
        components["sqlite"] = db_health
        if db_health["status"] != "healthy":
            overall_status = "unhealthy"
    except Exception as exc:
        components["sqlite"] = {"status": "unhealthy", "error": str(exc)}
        overall_status = "unhealthy"

    response = {
        "status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }

    status_code = 200 if overall_status == "healthy" else 503
    return JSONResponse(content=response, status_code=status_code)
