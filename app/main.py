import json
import logging
from contextlib import asynccontextmanager
import asyncio
from typing import Any, Dict, List, Set
from urllib import request
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from .llm import GroqLLM
from .config import Config
# from .mental_model import MentalModelFetcher
from .retriever_four import retrieve_records_planner, answer_with_snippets
from .db import get_neo4j_client, init_mongo_client, init_neo4j_client, attention_db_runtime, get_mongo_client, get_entry_point_files, get_repo_summary
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
    depth: int = 2
    entry_point: str = None  # Optional entry point file path
    current_level: int = 0  # Current depth level in the walkthrough


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


def dummy_gen():
    yield "This endpoint is under construction. Please check back later."

# @app.post("/query")
# async def query_repo(request: QueryRequest):
#     try:
#         mental_model = mental_model_fetcher.seed_prompt(request.repo_id)
#         print(f"Processing query for repo: {request.repo_id} with question: {request.question}")

#         repo_overview = {"overview": "Dictquery is a python library that allows users to query nested dictionaries using a simple DSL. It provides parsers and visitors to traverse and extract data from complex dictionary structures."}
#         out = retrieve_records_planner(
#             repo_name=request.repo_id,
#             question=request.question,
#             repo_overview=repo_overview,
#             llm=llm,
#         )

#         gen = answer_with_snippets(
#             question=request.question,
#             selection=out,
#             llm=llm,
#             repo_root=request.repo_id
#         )
        
#         return StreamingResponse(gen, media_type="text/markdown")


#     except Exception as e:
#         return {"error": str(e)}


# POST /ingest endpoint to ingest entire code repo folder
@app.post("/ingest")
async def ingest_repo(repo_path: str = "dictquery", job_id: str = None):
    local_repo_path = Path("data") / repo_path

    # Return HTTP status code 404 if repository not found
    if not local_repo_path.exists():
        return {"error": f"Repository not found: {local_repo_path}"}, 404

    asyncio.create_task(start_ingestion_pipeline(local_repo_path, job_id))
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
    return {"repos": ["data/dictquery", "data/xai-sdk-python", "data/fastapi"]}


@app.get("/repo/architecture")
async def get_repo_arch(repo_id: str):
    """Get the architecture overview for a specific repository."""
    try:
        architecture = await get_repo_architecture(repo_id)
        return architecture
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# Endpoints for repo walkthrough
@app.post("/walkthrough/repo/start")
async def start_walkthrough(request: WalkthroughRequest):
    db_client = get_mongo_client()
    entry_points = get_entry_point_files(db_client, request.repo_id)
    repo_summary = get_repo_summary(db_client, request.repo_id)
    clear_repo_walkthrough_sessions(request.repo_id)

    return {
        "entry_points": entry_points,
        "repo_summary": repo_summary,
        "repo_id": request.repo_id,
    }

@app.post("/walkthrough/repo/next")
async def walkthrough_next(request: WalkthroughRequest):
    return StreamingResponse(
        stream_walkthrough_next(
            request.repo_id, 
            request.depth, 
            request.entry_point, 
            request.current_level
        ),
        media_type="text/markdown"
    )

@app.post("/walkthrough/repo/plan")
async def walkthrough_repo_plan(req: WalkthroughRequest):
    plan = await build_repo_walkthrough_plan(
        repo_id=req.repo_id,
        depth=2,
    )
    return plan

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

@app.post("/walkthrough/goto")
async def goto_step(req: GotoRequest):
    return StreamingResponse(
        stream_walkthrough_goto(repo_id=req.repo_id, file_path=req.file_path),
        media_type="text/markdown"
    )