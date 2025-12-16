import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..config import Config
from ..db import get_mongo_client
from ..llm_grok import GrokLLM
from ..lsp_reference_resolver.run import CodeAnalyzer
from .create_repo_graph import ASTProcessorStage
from .mental_model_gen import MentalModelStage
from .pre_ingestion_analysis import PreIngestionAnalysisError, PreIngestionAnalysisStage

logger = logging.getLogger(__name__)


async def start_ingestion_pipeline(local_repo_path: Path, repo_name: str, job_id: Optional[str] = None) -> any:

    config = Config()
    llm = GrokLLM()
    mongo = get_mongo_client()

    job_id = job_id or str(uuid4())

    mongo.upsert_ingestion_job(
        job_id=job_id,
        repo_name=repo_name,
        status="running",
        current_stage="precheck",
        stage_status={
            "precheck": {"status": "pending"},
            "resolve_refs": {"status": "pending"},
            "ast": {"status": "pending"},
            "mental_model": {"status": "pending"},
        },
    )

    try:
        ## Pre-ingestion analysis
        pre_ingestion_stage = PreIngestionAnalysisStage(llm_grok=llm, job_id=job_id, repo_name=repo_name)
        analysis_summary = pre_ingestion_stage.run(repo_path=local_repo_path)
        logger.info("Pre-ingestion analysis summary: %s", analysis_summary)
        
    except PreIngestionAnalysisError as pie:
        mongo.upsert_ingestion_job(
            job_id=job_id,
            repo_name=repo_name,
            status="failed",
            stage_status={
                "precheck": {
                    "status": "failed",
                    "error": str(pie),
                    "metrics": getattr(pie, "metrics", None),
                }
            },
        )
        logger.warning("Pre-ingestion analysis failed: %s", pie)
        return

    except Exception as e:
        mongo.upsert_ingestion_job(
            job_id=job_id,
            repo_name=repo_name,
            status="failed",
            stage_status={
                "precheck": {
                    "status": "failed",
                    "error": str(e)
                }
            },
        )
        logger.exception("Pre-ingestion analysis failed unexpectedly")
        raise e

    try:
        changed_files: List[str] = await CodeAnalyzer(repo=local_repo_path, repo_id=repo_name, job_id=job_id).analyze()
        await ASTProcessorStage(job_id=job_id).run(local_path=local_repo_path, repo_id=repo_name, changed_files=changed_files)
        await MentalModelStage(llm_grok=llm, config={"job_id": job_id}).run(repo_id=repo_name, local_repo_path=local_repo_path)

        ## Add the repo name to the list of ingested repos in the DB
        mongo.add_ingested_repo(repo_name=repo_name, job_id=job_id)

        mongo.upsert_ingestion_job(
            job_id=job_id,
            repo_name=repo_name,
            status="completed",
        )

        return {"status": "completed"}

    except Exception as e:
        logger.exception("Error during ingestion pipeline for repo %s", repo_name)
        raise
