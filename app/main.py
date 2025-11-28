import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
import asyncio
from typing import List, Optional
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .chat_service import stream_chat
from .llm import GroqLLM
from .config import Config
from .db import get_neo4j_client, init_mongo_client, init_neo4j_client, get_mongo_client, get_entry_point_files, get_repo_summary
from pathlib import Path
from .repo_ingestion_pipeline import start_ingestion_pipeline
from .service import fetch_job_status
from .walkthrough_service import build_repo_walkthrough_plan, stream_walkthrough_goto, stream_walkthrough_next, clear_repo_walkthrough_sessions
from .walkthrough_def_service import Neo4jClient, build_definition_walkthrough_plan, stream_definition_walkthrough
from .repo_arch_service import get_repo_architecture

from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO)

Config.validate()
app = FastAPI(title="Code Repo QA Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_neo4j_client()
init_mongo_client()

llm = GroqLLM()
# mental_model_fetcher = MentalModelFetcher()

class RepoArchRequest(BaseModel):
    repo_id: str

class QueryRequest(BaseModel):
    question: str
    repo_id: str

class WalkthroughRequest(BaseModel):
    repo_id: str
    current_file_path: str = None  # Optional current file path
    entry_point: str = None  # Optional entry point file path
    current_level: int = 0  # Current depth level in the walkthrough
    depth: int = 2  # Max depth for the walkthrough


class DefWalkRequest(BaseModel):
    repo_id: str
    file_path: str
    definition_name: str
    depth: int = 2


class GetDefsRequest(BaseModel):
    repo_id: str
    file_path: str

class GotoRequest(BaseModel):
    repo_id: str
    file_path: str


class ChatRequest(BaseModel):
    conversation_id: str
    message: str

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

    result = conversations.insert_one(doc)

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
    mongo = get_mongo_client()
    conversations = mongo["conversations"]

    query: dict = {}
    if repo_id:
        query["repo_id"] = repo_id

    cursor = (
        conversations.find(query)
        .sort("updated_at", -1)
        .skip(offset)
        .limit(limit)
    )

    results: List[ConversationSummary] = []
    for doc in cursor:
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


@app.get("/conversations/{conversation_id}/messages",response_model=ConversationMessagesResponse)
async def list_conversation_messages(conversation_id: str, limit: int = 200):
    """
    Return all messages for a given conversation_id (up to `limit`).
    Used by the UI to reload an existing chat.
    """
    mongo = get_mongo_client()
    conversations = mongo["conversations"]
    messages_col = mongo["messages"]

    # Ensure the conversation exists
    try:
        conv = conversations.find_one({"_id": ObjectId(conversation_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    cursor = (
        messages_col.find({"conversation_id": conversation_id})
        .sort("created_at", 1)
        .limit(limit)
    )

    messages: List[MessageModel] = []
    for m in cursor:
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
    """
    Delete a conversation and all its messages.
    """
    mongo = get_mongo_client()
    conversations = mongo["conversations"]
    messages_col = mongo["messages"]

    # Ensure the conversation exists
    try:
        conv = conversations.find_one({"_id": ObjectId(conversation_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid conversation id")

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Delete messages
    messages_col.delete_many({"conversation_id": conversation_id})

    # Delete conversation
    conversations.delete_one({"_id": ObjectId(conversation_id)})

    return {"message": "Conversation deleted"}

@app.post("/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(
        stream_chat(conversation_id=req.conversation_id, user_message=req.message),
        media_type="text/markdown"
    )

# POST /ingest endpoint to ingest entire code repo folder
@app.post("/ingest")
async def ingest_repo(repo_name: str, job_id: str = None):
    local_repo_path = Path("data") / repo_name

    repo_name = f"data/{repo_name}"
    # Return HTTP status code 404 if repository not found
    if not local_repo_path.exists():
        return {"error": f"Repository not found: {local_repo_path}"}, 404

    asyncio.create_task(start_ingestion_pipeline(local_repo_path=local_repo_path, repo_name=repo_name, job_id=job_id))
    return {"message": f"Job {job_id} started. Check terminal for results"}


@app.get("/status/{job_id}")
async def get_job_status(job_id: str):
    """Get the status of a specific job."""

    # Call fetch_job_status and check if it returns a result
    try:
        job_status = fetch_job_status(job_id)
        return job_status
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Job not found")
    

# Create GET /repos endpoint
@app.get("/repos")
async def list_repos():
    """List all ingested code repositories."""
    # Implement logic to retrieve and return the list of repositories
    return {"repos": ["data/dictquery", "data/xai-sdk-python", "data/fastapi", "data/nanochat", "data/the-algorithm"]}


@app.get("/repo/architecture")
async def get_repo_arch(repo_id: str):
    """Get the architecture overview for a specific repository."""
    try:
        architecture = await get_repo_architecture(repo_id)
        return architecture
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# Endpoints for repo walkthrough
@app.post("/repo/walkthrough/start")
async def start_walkthrough(request: WalkthroughRequest):
    db_client = get_mongo_client()
    entry_points = get_entry_point_files(db_client, request.repo_id)
    # repo_summary = get_repo_summary(db_client, request.repo_id)
    clear_repo_walkthrough_sessions(request.repo_id)

    return {
        "entry_points": entry_points,
        # "repo_summary": repo_summary,
        "repo_id": request.repo_id,
    }

@app.post("/repo/walkthrough/next")
async def walkthrough_next(request: WalkthroughRequest):
    return StreamingResponse(
        stream_walkthrough_next(
            repo_id=request.repo_id,
            current_file_path=request.current_file_path,
            entry_point=request.entry_point,
            level=request.current_level
        ),
        media_type="text/markdown"
    )

@app.post("/repo/walkthrough/plan")
async def walkthrough_repo_plan(req: WalkthroughRequest):
    plan = await build_repo_walkthrough_plan(
        repo_id=req.repo_id,
        depth=req.depth,
    )
    return plan


@app.post("/repo/walkthrough/goto")
async def goto_step(req: GotoRequest):
    return StreamingResponse(
        stream_walkthrough_goto(repo_id=req.repo_id, file_path=req.file_path),
        media_type="text/markdown"
    )

@app.post("/walkthrough/def/start")
async def walkthrough_def_start(req: DefWalkRequest):
    return StreamingResponse(
        stream_definition_walkthrough(
            repo_id=req.repo_id,
            file_path=req.file_path,
            definition_name=req.definition_name,
            depth=req.depth,
        ),
        media_type="text/markdown"
    )


@app.post("/walkthrough/def/plan")
async def walkthrough_def_plan(req: DefWalkRequest):
    """
    Return a plan (graph + sequence) for walking through a definition.
    UI can render this as Def A -> Def B -> Def C with depth levels.
    """
    plan = await build_definition_walkthrough_plan(
        repo_id=req.repo_id,
        file_path=req.file_path,
        definition_name=req.definition_name,
        depth=req.depth,
    )
    return plan


@app.get("/defs")
async def list_definitions(repo_id: str, file_path: str):
    """
    List all definitions (functions/classes) for a given repo + file.
    Used by the UI to let the user pick which definition to start a walkthrough from.
    """
    neo: Neo4jClient = get_neo4j_client()
    nodes = neo._fetch_def_nodes(repo_id)

    # Filter only for the requested file
    file_defs = [n for n in nodes if n["file_path"] == file_path]

    results = []
    for n in file_defs:
        symbol = ""
        try:
            symbol = neo._fetch_symbol_from_ast(n["node_id"]) or ""
        except Exception:
            symbol = ""
        results.append({
            "node_id": n["node_id"],
            "symbol": symbol,
            "file_path": n["file_path"],
            "node_type": n["node_type"],
            "start_line": n["start_line"],
            "end_line": n["end_line"],
        })

    return {
        "repo_id": repo_id,
        "file_path": file_path,
        "definitions": results
    }