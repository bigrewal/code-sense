import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from bson import ObjectId
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query  
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .models.data_model import JobAborted, IngestionStage, IngestionStageStatus, IngestionJobStatus
from .utils import get_repo_path, get_repo_dir, now_ts
from .chat_service import stream_chat, stateless_stream_chat
from .config import Config, validate_required_settings
from .db import (
    get_mongo_client,
    get_neo4j_client,
    init_mongo_client,
    init_neo4j_client,
)
from .repo_ingestion_pipeline import start_ingestion_pipeline

MAX_LIMIT = 200

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Code Sense API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    logger.info("Initializing clients")
    validate_required_settings()
    init_neo4j_client()
    init_mongo_client()


@app.on_event("shutdown")
async def on_shutdown():
    """
    Graceful shutdown: close database connections and cleanup resources.
    """
    logger.info("Shutting down application - closing database connections")

    try:
        neo4j_client = get_neo4j_client()
        if neo4j_client:
            neo4j_client.close()
            logger.info("Neo4j connection closed")
    except Exception as e:
        logger.error("Error closing Neo4j connection: %s", str(e))

    try:
        mongo_client = get_mongo_client()
        if mongo_client:
            mongo_client.close()
            logger.info("MongoDB connection closed")
    except Exception as e:
        logger.error("Error closing MongoDB connection: %s", str(e))

    logger.info("Shutdown complete")


class ChatRequest(BaseModel):
    conversation_id: str
    message: str

class StatelessChatRequest(BaseModel):
    repo_id: str
    message: str

class ErrorResponse(BaseModel):
    detail: str

class ConversationCreateRequest(BaseModel):
    repo_id: str


class ConversationCreateResponse(BaseModel):
    conversation_id: str
    repo_id: str
    created_at: datetime


class ConversationSummary(BaseModel):
    conversation_id: str
    repo_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    title: Optional[str] = None


class MessageModel(BaseModel):
    role: str
    content: str
    created_at: datetime


class ConversationMessagesResponse(BaseModel):
    conversation_id: str
    messages: List[MessageModel]

class IngestRequest(BaseModel):
    repo_names: List[str]


@app.post("/conversations", response_model=ConversationCreateResponse)
async def create_conversation(req: ConversationCreateRequest):
    """
    Create a new chat conversation for a given repo.
    Frontend calls this once when the user clicks 'New Chat'.
    """
    mongo = get_mongo_client()

    result = mongo.create_conversation(repo_id=req.repo_id)

    return ConversationCreateResponse(
        conversation_id=result["conversation_id"],
        repo_id=result["repo_id"],
        created_at=result["created_at"],
    )


@app.get("/conversations", response_model=List[ConversationSummary])
async def list_conversations(
    repo_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    mongo = get_mongo_client()

    docs = await asyncio.to_thread(
        mongo.list_conversations,
        repo_id=repo_id,
        limit=limit,
        offset=offset,
    )

    return [
        ConversationSummary(
            conversation_id=str(d["_id"]),
            repo_id=d.get("repo_id"),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            title=d.get("title"),
        )
        for d in docs
    ]


@app.get("/conversations/{conversation_id}/messages", response_model=ConversationMessagesResponse)
async def list_conversation_messages(conversation_id: str, limit: int = Query(200, ge=1, le=500)):
    mongo = get_mongo_client()

    try:
        exists = await asyncio.to_thread(mongo.conversation_exists, conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    if not exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    docs = await asyncio.to_thread(
        mongo.list_conversation_messages,
        conversation_id=conversation_id,
        limit=limit,
    )

    messages: List[MessageModel] = [
        MessageModel(
            role=m["role"],
            content=m["content"],
            created_at=m.get("created_at"),
        )
        for m in docs
    ]

    return ConversationMessagesResponse(conversation_id=conversation_id, messages=messages)


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    mongo = get_mongo_client()

    try:
        await asyncio.to_thread(mongo.delete_conversation, conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid conversation id")
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
        media_type="text/markdown",
    )


@app.post("/stateless/chat", responses={400: {"model": ErrorResponse}})
async def stateless_chat(req: StatelessChatRequest):
    if not req.repo_id:
        raise HTTPException(status_code=400, detail="repo_id is required")
    if not req.message:
        raise HTTPException(status_code=400, detail="message is required")

    return StreamingResponse(
        stateless_stream_chat(repo_id=req.repo_id, user_message=req.message),
        media_type="text/markdown",
    )


@app.post("/ingest", responses={404: {"model": ErrorResponse}})
async def ingest_repo(
    background_tasks: BackgroundTasks,
    repo_names: IngestRequest = None,
):
    if len(repo_names.repo_names) < 1:
        raise HTTPException(status_code=400, detail="Provide repo_names")

    repos = repo_names.repo_names

    # Validate repos exist and build paths
    repo_paths: List[Path] = []
    repo_full_names: List[str] = []
    mongo = get_mongo_client()
    for r in repos:
        logger.info(f"Processing repo for ingestion: {r}")
        local_repo_path = get_repo_path(r)
        if not local_repo_path.exists():
            raise HTTPException(status_code=404, detail=f"Repository not found: {local_repo_path}")
        
        # Check if the repo has already been ingested
        repo_full_name = get_repo_dir(r)
        if mongo.is_repo_ingested(repo_full_name) or mongo.is_repo_being_ingested(repo_full_name):
            raise HTTPException(status_code=400, detail=f"Repository already ingested or being ingested: {r}")

        repo_paths.append(local_repo_path)
        repo_full_names.append(repo_full_name)

    # Multiple repos: create batch + queued jobs, run sequentially in ONE task
    batch_id = str(uuid4())
    jobs = []

    for idx, (path, full_name) in enumerate(zip(repo_paths, repo_full_names)):
        job_id = str(uuid4())

        job = IngestionJobStatus(
            job_id=job_id,
            repo_name=full_name,
            status="queued",
            current_stage=IngestionStage.PRECHECK,
            stage_status={
                IngestionStage.PRECHECK: IngestionStageStatus.PENDING,
                IngestionStage.RESOLVE_REFS: IngestionStageStatus.PENDING,
                IngestionStage.REPO_GRAPH: IngestionStageStatus.PENDING,
                IngestionStage.MENTAL_MODEL: IngestionStageStatus.PENDING,
            },
        )

        mongo.upsert_ingestion_job(job, extra_fields={"batch_id": batch_id, "batch_index": idx})
        jobs.append({"job_id": job_id, "repo_name": full_name, "status": "queued", "batch_index": idx})

    background_tasks.add_task(run_batch_sequentially, batch_id=batch_id)

    return {"batch_id": batch_id, "jobs": jobs}


async def run_batch_sequentially(batch_id: str):
    mongo = get_mongo_client()

    jobs = list(
        mongo._db["ingestion_jobs"]
        .find({"batch_id": batch_id}, {"_id": 0})
        .sort("batch_index", 1)
    )

    for job_doc in jobs:
        job_id = job_doc["job_id"]
        repo_name = job_doc["repo_name"]
        local_repo_path = Path(repo_name)

        # If user aborted while queued, mark and skip
        if mongo.is_abort_requested(job_id):
            mongo.upsert_ingestion_job(
                IngestionJobStatus(
                    job_id=job_id,
                    repo_name=repo_name,
                    status="aborted",
                    current_stage=IngestionStage.PRECHECK,
                    stage_status={IngestionStage.PRECHECK: IngestionStageStatus.ABORTED},
                ),
                extra_fields={"aborted_before_start": True, "aborted_at": now_ts()},
            )
            continue

        try:
            await start_ingestion_pipeline(
                local_repo_path=local_repo_path,
                repo_name=repo_name,
                job_id=job_id,
            )

        except JobAborted:
            # already marked aborted in pipeline
            continue
        except Exception as e:
            logger.exception("Ingestion pipeline failed", exc_info=e)
            continue

@app.delete("/repos", responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def delete_repo(repo_path: str, delete_files: bool = False):
    """Delete a code repository and its associated data."""
    local_repo_path = Path(repo_path)

    if not local_repo_path.exists():
        raise HTTPException(status_code=404, detail=f"Repository not found: {local_repo_path}")

    if delete_files:
        try:
            # Guarded removal; keeps defaults safe
            import shutil

            shutil.rmtree(local_repo_path)
        except Exception as exc:
            logger.exception("Failed to delete repo files", exc_info=exc)
            raise HTTPException(status_code=500, detail="Failed to delete repo files")

    mongo = get_mongo_client()

    try:
        mongo.delete_repo_data(repo_path)
    except Exception as exc:
        logger.exception("Failed to delete repo documents", exc_info=exc)
        raise HTTPException(status_code=500, detail="Failed to delete repo data")

    return {"message": f"Repository {repo_path} and its data have been deleted."}


@app.get("/status/batch/{batch_id}")
async def get_batch_status(batch_id: str):
    mongo = get_mongo_client()

    jobs = await asyncio.to_thread(mongo.get_batch_jobs, batch_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="Batch not found")

    return {"batch_id": batch_id, "jobs": jobs}


@app.get("/status")
async def get_status(
    job_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    status: Optional[str] = None,
    repo_name: Optional[str] = None,
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    skip: int = Query(0, ge=0),
):
    mongo = get_mongo_client()

    if job_id:
        job = mongo.get_job_status(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    jobs, total = mongo.list_jobs(
        batch_id=batch_id,
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
        "batch_id": batch_id,
        "skip": skip,
        "limit": limit,
    }


@app.post("/jobs/{job_id}/abort")
async def abort_job(job_id: str):
    mongo = get_mongo_client()
    ok = mongo.request_abort(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "abort_requested": True,
    }

@app.get("/repos")
async def list_repos():
    """List all ingested code repositories."""

    mongo = get_mongo_client()
    ingested_repos = mongo.list_ingested_repos()
    return {"repos": ingested_repos}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    mongo = get_mongo_client()

    job = await asyncio.to_thread(mongo.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.get("status") in {"running", "aborting"}:
        raise HTTPException(
            status_code=409,
            detail="Job is running. Abort the job before deleting.",
        )

    ok = await asyncio.to_thread(mongo.delete_job, job_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to delete job")

    return {"job_id": job_id, "deleted": True}


@app.get("/health")
async def health():
    """
    Comprehensive health check endpoint.

    Returns:
        - 200: All systems healthy
        - 503: One or more systems unhealthy

    Response includes:
        - Overall status
        - Component health (Neo4j, MongoDB)
        - Response times
        - Connection pool status
        - Metrics (if enabled)
    """
    from fastapi.responses import JSONResponse

    overall_status = "healthy"
    components = {}

    # Check Neo4j
    try:
        neo4j_client = get_neo4j_client()
        neo4j_health = neo4j_client.health_check()
        components["neo4j"] = neo4j_health
        if neo4j_health["status"] != "healthy":
            overall_status = "unhealthy"
    except Exception as e:
        components["neo4j"] = {"status": "unhealthy", "error": str(e)}
        overall_status = "unhealthy"

    # Check MongoDB
    try:
        mongo_client = get_mongo_client()
        mongo_health = mongo_client.health_check()
        components["mongodb"] = mongo_health
        if mongo_health["status"] != "healthy":
            overall_status = "unhealthy"
    except Exception as e:
        components["mongodb"] = {"status": "unhealthy", "error": str(e)}
        overall_status = "unhealthy"

    # Include metrics if enabled
    if Config.ENABLE_DB_METRICS:
        from .db import _db_metrics
        components["metrics"] = _db_metrics.get_summary()

    response = {
        "status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }

    status_code = 200 if overall_status == "healthy" else 503
    return JSONResponse(content=response, status_code=status_code)


@app.get("/metrics")
async def metrics():
    """
    Expose database operation metrics.

    Returns metrics including:
        - Operation counts by type
        - Error counts by type
        - Slow query count
        - Average response times

    This endpoint is useful for monitoring and alerting.
    Can be integrated with Prometheus, Grafana, etc.
    """
    if not Config.ENABLE_DB_METRICS:
        raise HTTPException(status_code=404, detail="Metrics disabled")

    from .db import _db_metrics

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": _db_metrics.get_summary(),
    }
