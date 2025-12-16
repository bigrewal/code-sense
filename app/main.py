import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from bson import ObjectId
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .chat_service import stream_chat
from .config import validate_required_settings
from .db import (
    get_mongo_client,
    get_neo4j_client,
    init_mongo_client,
    init_neo4j_client,
)
from .repo_ingestion_pipeline import start_ingestion_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Code Repo QA Agent")

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
    logger.info("Shutting down app")


class ChatRequest(BaseModel):
    conversation_id: str
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


@app.post("/conversations", response_model=ConversationCreateResponse)
async def create_conversation(req: ConversationCreateRequest):
    """
    Create a new chat conversation for a given repo.
    Frontend calls this once when the user clicks 'New Chat'.
    """
    mongo = get_mongo_client()
    conversations = mongo["conversations"]

    now = datetime.utcnow()

    doc = {
        "repo_id": req.repo_id,
        "created_at": now,
        "updated_at": now,
        "type": "REPO_CHAT",
    }

    result = await asyncio.to_thread(conversations.insert_one, doc)

    return ConversationCreateResponse(
        conversation_id=str(result.inserted_id),
        repo_id=req.repo_id,
        created_at=now,
    )


@app.get("/conversations", response_model=List[ConversationSummary])
async def list_conversations(
    repo_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """
    List conversations, optionally filtered by repo_id.
    Used by the UI to populate the 'chat sidebar'.
    """
    limit = max(1, min(limit, 200))
    mongo = get_mongo_client()
    conversations = mongo["conversations"]

    query: dict = {}
    if repo_id:
        query["repo_id"] = repo_id

    cursor = (
        conversations.find(query, projection={"title": 1, "repo_id": 1, "created_at": 1, "updated_at": 1})
        .sort("updated_at", -1)
        .skip(offset)
        .limit(limit)
    )

    docs = await asyncio.to_thread(list, cursor)
    results: List[ConversationSummary] = []
    for doc in docs:
        results.append(
            ConversationSummary(
                conversation_id=str(doc["_id"]),
                repo_id=doc["repo_id"],
                created_at=doc.get("created_at"),
                updated_at=doc.get("updated_at"),
                title=doc.get("title"),
            )
        )

    return results


@app.get("/conversations/{conversation_id}/messages", response_model=ConversationMessagesResponse)
async def list_conversation_messages(conversation_id: str, limit: int = 200):
    """
    Return all messages for a given conversation_id (up to `limit`).
    Used by the UI to reload an existing chat.
    """
    limit = max(1, min(limit, 500))
    mongo = get_mongo_client()
    conversations = mongo["conversations"]
    messages_col = mongo["messages"]

    # Ensure the conversation exists
    try:
        conv = await asyncio.to_thread(
            conversations.find_one, {"_id": ObjectId(conversation_id)}, {"_id": 1}
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    cursor = (
        messages_col.find({"conversation_id": conversation_id}, {"_id": 0})
        .sort("created_at", 1)
        .limit(limit)
    )

    messages: List[MessageModel] = []
    docs = await asyncio.to_thread(list, cursor)
    for m in docs:
        messages.append(
            MessageModel(
                role=m["role"],
                content=m["content"],
                created_at=m.get("created_at"),
            )
        )

    return ConversationMessagesResponse(
        conversation_id=conversation_id,
        messages=messages,
    )


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """Delete a conversation and all its messages."""
    mongo = get_mongo_client()
    conversations = mongo["conversations"]
    messages_col = mongo["messages"]

    try:
        conv = await asyncio.to_thread(conversations.find_one, {"_id": ObjectId(conversation_id)}, {"_id": 1})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    await asyncio.to_thread(messages_col.delete_many, {"conversation_id": conversation_id})
    await asyncio.to_thread(conversations.delete_one, {"_id": ObjectId(conversation_id)})

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


@app.post("/ingest", responses={404: {"model": ErrorResponse}})
async def ingest_repo(repo_name: str, background_tasks: BackgroundTasks, job_id: Optional[str] = None):
    """Kick off ingestion of a repo in the background."""
    local_repo_path = Path("data") / repo_name

    repo_name_full = f"data/{repo_name}"
    if not local_repo_path.exists():
        raise HTTPException(status_code=404, detail=f"Repository not found: {local_repo_path}")

    job_identifier = job_id or str(uuid4())

    background_tasks.add_task(
        start_ingestion_pipeline,
        local_repo_path=local_repo_path,
        repo_name=repo_name_full,
        job_id=job_identifier,
    )

    return {"message": f"Job {job_identifier} started. Check terminal for results"}


@app.delete("/repos/{repo_name}", responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def delete_repo(repo_name: str, delete_files: bool = False):
    """Delete a code repository and its associated data."""
    if Path(repo_name).name != repo_name:
        raise HTTPException(status_code=400, detail="Invalid repository name")

    local_repo_path = Path("data") / repo_name

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
    neo4j = get_neo4j_client()

    repo_id = f"data/{repo_name}"
    try:
        mongo.delete_repo_data(repo_id)
    except Exception as exc:
        logger.exception("Failed to delete repo documents", exc_info=exc)
        raise HTTPException(status_code=500, detail="Failed to delete repo data")

    try:
        with neo4j.driver.session() as neo_session:
            neo4j.clear_repo_data(session=neo_session, repo_id=repo_id)
    except Exception as exc:
        logger.exception("Failed to delete repo graph", exc_info=exc)
        raise HTTPException(status_code=500, detail="Failed to delete graph data")

    return {"message": f"Repository {repo_name} and its data have been deleted."}


@app.get("/status/{job_id}")
async def get_job_status(job_id: str):
    """Get the status of a specific job."""
    mongo = get_mongo_client()
    try:
        job_status = mongo.get_job_status(job_id)
        return job_status
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found")
    

# Create GET /repos endpoint
@app.get("/repos")
async def list_repos():
    """List all ingested code repositories."""

    mongo = get_mongo_client()
    ingested_repos = mongo.list_ingested_repos()
    return {"repos": ingested_repos}


@app.get("/health")
async def health():
    return {"status": "ok"}
