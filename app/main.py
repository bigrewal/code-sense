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
from .planner import QueryPlanner
from .planner_two import execute_route
from .planner_three import FreeformQA
from .retriever_four import retrieve_records_planner, Neo4jHooks
from .router import RoutePlan, Router
from .executor import PlanExecutor
from .synthesizer import ResponseSynthesizer
from .db import init_mongo_client, init_neo4j_client, attention_db_runtime
from pathlib import Path
from .repo_ingestion_pipeline import start_ingestion_pipeline
from .service import fetch_job_status

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
planner = QueryPlanner(llm)
executor = PlanExecutor(llm)
synthesizer = ResponseSynthesizer(llm)


class QueryRequest(BaseModel):
    question: str
    repo_id: str


async def stream_response(question: str, mental_model: Dict[str, str], execution_results: List[Dict[str, Any]]):
    async for chunk in synthesizer.synthesize(question, mental_model, execution_results):
        yield f"event: message\ndata: {json.dumps(chunk)}\n\n"


def dummy_gen():
    yield "This endpoint is under construction. Please check back later."

@app.post("/query")
async def query_repo(request: QueryRequest):
    try:
        mental_model = mental_model_fetcher.seed_prompt(request.repo_id)
        # print("Mental model fetched and prompt seeded. \n ", mental_model)
        # mental_model = mental_model_fetcher.fetch()
        # pack_md, pack_items = await attention_db_runtime.pack(request.repo_id, request.question)
        # print("Packed items:", pack_items)

        # plan, mental_model_summary = await planner.plan(request.question, mental_model)
        # execution_results = await executor.execute(plan, request.question, mental_model, request.repo_id, parallel=True)

        # router: Router = Router(llm)
        # route: RoutePlan = router.route(user_question=request.question, seed_prompt=mental_model, repo_name=request.repo_id)

        # gen = execute_route(
        #     route_plan=route,
        #     llm=llm,
        #     repo_name=request.repo_id,
        #     seed_prompt=mental_model,
        #     user_question=request.question,
        # )

        print(f"Processing query for repo: {request.repo_id} with question: {request.question}")

        class StubNeo(Neo4jHooks):
            def upstream(self, file_path: str) -> List[str]:
                return []
            def downstream(self, file_path: str) -> List[str]:
                return []
            def scc_closure(self, files: Set[str]) -> Set[str]:
                return set(files)

        repo_overview = {"overview": "Dictquery is a python library that allows users to query nested dictionaries using a simple DSL. It provides parsers and visitors to traverse and extract data from complex dictionary structures."}
        out = retrieve_records_planner(
            repo_name=request.repo_id,
            question=request.question,
            repo_overview=repo_overview,
            llm=llm,
            neo=StubNeo(),
        )

        print(json.dumps(out, indent=2))

        # fqa = FreeformQA(
        #     llm=llm,
        #     repo_name=request.repo_id,
        #     seed_prompt=mental_model,
        #     user_question=request.question,
        # )
        # gen = fqa.answer()
        
        return StreamingResponse(dummy_gen(), media_type="text/markdown")
        # walkthrough_gen = walkthrough_generator_for_fastapi(
        #         llm=llm,  # your injected client
        #         repo_name=request.repo_id,
        #         seed_prompt=mental_model,
        #         user_question=request.question,
        #     )

        # async for chunk in synthesizer.synthesize(request.question, mental_model_summary, execution_results):
        #     print(chunk, end="")
        
        # return {
        #     "response": "Check the console for the streamed response."
        # }

        # return {
        #     "response": "Check the console for the streamed response."
        # }
        # for chunk in walkthrough_gen:
        #     print(chunk, end="")

        # return StreamingResponse(
        #     walkthrough_gen,
        #     media_type="text/markdown",
        #     headers={
        #         "Cache-Control": "no-cache",
        #         "Connection": "keep-alive",
        #         # Optional but often useful:
        #         "X-Accel-Buffering": "no",
        #     },
        # )
        # return StreamingResponse(
        #     stream_response(
        #         request.question,
        #         mental_model_summary,  # send the summary you synthesized with
        #         execution_results
        #     ),
        #     media_type="text/event-stream",
        #     headers={
        #         "Cache-Control": "no-cache",
        #         "Connection": "keep-alive",
        #         # Optional but often useful:
        #         "X-Accel-Buffering": "no",
        #     },
        # )

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
