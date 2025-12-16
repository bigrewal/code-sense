from typing import Any, Dict, List

from ..lsp_reference_resolver.run import CodeAnalyzer
from ..config import Config
from .resolve_references import ReferenceResolver
from .create_repo_graph import ASTProcessorStage
from .mental_model_gen import MentalModelStage
from .pre_ingestion_analysis import PreIngestionAnalysisStage, PreIngestionAnalysisError
from ..db import get_mongo_client
from ..llm_grok import GrokLLM
from pathlib import Path


async def start_ingestion_pipeline(local_repo_path: Path, repo_name: str, job_id: str) -> any:
    # pipeline = Pipeline(config)

    config = Config()
    llm = GrokLLM()
    mongo = get_mongo_client()

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
        print(f"Pre-ingestion analysis summary: {analysis_summary}")
        
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
        print(f"Pre-ingestion analysis failed: {pie}")
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
        print(f"Pre-ingestion analysis failed: {e}")
        raise e

    try:
        changed_files: List[str] = await CodeAnalyzer(repo=local_repo_path, repo_id=repo_name, job_id=job_id).analyze()
        await ASTProcessorStage({"job_id": job_id, **config.AST_CONFIG}).run(local_path=local_repo_path, repo_id=repo_name, changed_files=changed_files)
        await MentalModelStage(llm_grok=llm, config={"job_id": job_id}).run(repo_id=repo_name, local_repo_path=local_repo_path)

        ## Add the repo name to the list of ingested repos in the DB
        mongo.add_ingested_repo(repo_name=repo_name, job_id=job_id)

        mongo.upsert_ingestion_job(
            job_id=job_id,
            repo_name=repo_name,
            status="completed",
        )
        mongo.add_ingested_repo(repo_name, job_id)


        return {"status": "completed"}

    except Exception as e:
        print(f"Error during ingestion pipeline: {e}")
        raise e