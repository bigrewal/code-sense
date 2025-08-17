import json
import logging
from contextlib import asynccontextmanager
import asyncio
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from .llm import GroqLLM
from .config import Config
from .mental_model import MentalModelFetcher
from .planner import QueryPlanner
from .executor import PlanExecutor
from .synthesizer import ResponseSynthesizer
from .db import init_mongo_client, init_neo4j_client
from pathlib import Path
from .repo_ingestion_pipeline import start_ingestion_pipeline
from .service import fetch_job_status

logging.basicConfig(level=logging.INFO)

Config.validate()
app = FastAPI(title="Code Repo QA Agent")

init_neo4j_client() 
init_mongo_client()

llm = GroqLLM()
mental_model_fetcher = MentalModelFetcher()
planner = QueryPlanner(llm)
executor = PlanExecutor(llm)
synthesizer = ResponseSynthesizer(llm)

class QueryRequest(BaseModel):
    question: str

async def stream_response(question: str, mental_model: Dict[str, str], plan: List[Dict[str, Any]], execution_results: List[Dict[str, Any]]):
    async for chunk in synthesizer.synthesize(question, mental_model, execution_results):
        yield f"event: message\ndata: {json.dumps(chunk)}\n\n"

@app.post("/query")
async def query_repo(request: QueryRequest):
    try:
        mental_model = mental_model_fetcher.fetch()
        plan, mental_model_summary = await planner.plan(request.question, mental_model)
        execution_results = await executor.execute(plan, request.question, mental_model, parallel=True)

        async for chunk in synthesizer.synthesize(request.question, mental_model_summary, execution_results):
            print(chunk, end="")
        

        return {
            "response": "Check the console for the streamed response."
        }

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
