import json
import logging
from contextlib import asynccontextmanager
import asyncio
from typing import Any, Dict, List, Set
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from .llm import GroqLLM
from .config import Config
from .mental_model import MentalModelFetcher
from .retriever_four import retrieve_records_planner, answer_with_snippets
from .executor import PlanExecutor
from .db import init_mongo_client, init_neo4j_client, attention_db_runtime, get_mongo_client, get_entry_point_files, get_repo_summary
from pathlib import Path
from .repo_ingestion_pipeline import start_ingestion_pipeline
from .service import fetch_job_status
from .walkthrough_service import stream_walkthrough_next
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
mental_model_fetcher = MentalModelFetcher()
executor = PlanExecutor(llm)

class QueryRequest(BaseModel):
    question: str
    repo_id: str

class WalkthroughRequest(BaseModel):
    repo_id: str

# async def stream_response(question: str, mental_model: Dict[str, str], execution_results: List[Dict[str, Any]]):
#     async for chunk in synthesizer.synthesize(question, mental_model, execution_results):
#         yield f"event: message\ndata: {json.dumps(chunk)}\n\n"


def dummy_gen():
    yield "This endpoint is under construction. Please check back later."

@app.post("/query")
async def query_repo(request: QueryRequest):
    try:
        mental_model = mental_model_fetcher.seed_prompt(request.repo_id)
        print(f"Processing query for repo: {request.repo_id} with question: {request.question}")

        repo_overview = {"overview": "Dictquery is a python library that allows users to query nested dictionaries using a simple DSL. It provides parsers and visitors to traverse and extract data from complex dictionary structures."}
        out = retrieve_records_planner(
            repo_name=request.repo_id,
            question=request.question,
            repo_overview=repo_overview,
            llm=llm,
        )

        gen = answer_with_snippets(
            question=request.question,
            selection=out,
            llm=llm,
            repo_root=request.repo_id
        )
        
        return StreamingResponse(gen, media_type="text/markdown")


    except Exception as e:
        return {"error": str(e)}


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


# Endpoints for repo walkthrough
@app.post("/walkthrough/start")
async def start_walkthrough(request: WalkthroughRequest):
    db_client = get_mongo_client()
    entry_points = get_entry_point_files(db_client, request.repo_id)
    repo_summary = get_repo_summary(db_client, request.repo_id)

    return {
        "entry_points": entry_points,
        "repo_summary": repo_summary,
        "repo_id": request.repo_id,
    }

@app.post("/walkthrough/next")
async def walkthrough_next(request: WalkthroughRequest):
    return StreamingResponse(
        stream_walkthrough_next(request.repo_id),
        media_type="text/markdown"
    )