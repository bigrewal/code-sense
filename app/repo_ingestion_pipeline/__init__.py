import logging
from pathlib import Path
from time import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..models.data_model import JobAborted, IngestionJobStatus, IngestionStage, IngestionStageStatus
from ..db import get_mongo_client
from ..utils import now_ts
from ..llm_grok import GrokLLM
from ..lsp_reference_resolver.run import CodeAnalyzer
from .create_repo_graph import CreateRepoGraphStage
from .mental_model_gen import MentalModelStage
from .pre_ingestion_analysis import PreIngestionAnalysisError, PreIngestionAnalysisStage

logger = logging.getLogger(__name__)
mongo = get_mongo_client()

def _abort_if_requested(job_id: str, repo_name: str, stage: IngestionStage):
    if mongo.is_abort_requested(job_id):
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="aborted",
                current_stage=stage,
                stage_status={stage: IngestionStageStatus.ABORTED},
            ),
            extra_fields={
                "abort_requested": True,
                "aborted_at": now_ts(),
                "aborted_after_stage": stage.value,
            },
        )
        raise JobAborted()


async def start_ingestion_pipeline(
    local_repo_path: Path,
    repo_name: str,
    job_id: Optional[str] = None,
) -> any:
    llm = GrokLLM()

    job_id = job_id or str(uuid4())

    # Initialize canonical stages (no string stage names)
    mongo.upsert_ingestion_job(
        IngestionJobStatus(
            job_id=job_id,
            repo_name=repo_name,
            status="running",
            current_stage=IngestionStage.PRECHECK,
            stage_status={
                IngestionStage.PRECHECK: IngestionStageStatus.PENDING,
                IngestionStage.RESOLVE_REFS: IngestionStageStatus.PENDING,
                IngestionStage.REPO_GRAPH: IngestionStageStatus.PENDING,
                IngestionStage.MENTAL_MODEL: IngestionStageStatus.PENDING,
            },
        )
    )

    # ---------- PRECHECK ----------
    _abort_if_requested(job_id, repo_name, IngestionStage.PRECHECK)
    try:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.PRECHECK,
                stage_status={IngestionStage.PRECHECK: IngestionStageStatus.RUNNING},
            )
        )

        pre_ingestion_stage = PreIngestionAnalysisStage(
            llm_grok=llm, job_id=job_id, repo_name=repo_name
        )
        analysis_summary = pre_ingestion_stage.run(repo_path=local_repo_path)

        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.PRECHECK,
                stage_status={
                    IngestionStage.PRECHECK: {
                        "status": IngestionStageStatus.COMPLETED.value,
                        "metrics": analysis_summary,
                    }
                },
            )
        )

    except PreIngestionAnalysisError as pie:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="failed",
                current_stage=IngestionStage.PRECHECK,
                stage_status={
                    IngestionStage.PRECHECK: {
                        "status": IngestionStageStatus.FAILED.value,
                        "error": str(pie),
                    }
                },
            ),
            error=str(pie),
        )
        return
    except Exception as e:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="failed",
                current_stage=IngestionStage.PRECHECK,
                stage_status={
                    IngestionStage.PRECHECK: {
                        "status": IngestionStageStatus.FAILED.value,
                        "error": str(e),
                    }
                },
            ),
            error=str(e),
        )
        raise

    # ---------- RESOLVE / ANALYZE ----------
    _abort_if_requested(job_id, repo_name, IngestionStage.RESOLVE_REFS)
    try:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.RESOLVE_REFS,
                stage_status={IngestionStage.RESOLVE_REFS: IngestionStageStatus.RUNNING},
            )
        )

        changed_files: List[str] = await CodeAnalyzer(
            repo=local_repo_path, repo_id=repo_name, job_id=job_id
        ).analyze()

        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.RESOLVE_REFS,
                stage_status={
                    IngestionStage.RESOLVE_REFS: {
                        "status": IngestionStageStatus.COMPLETED.value,
                    }
                },  
            )
        )
    except JobAborted:
        return
    
    except Exception as e:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="failed",
                current_stage=IngestionStage.RESOLVE_REFS,
                stage_status={
                    IngestionStage.RESOLVE_REFS: {
                        "status": IngestionStageStatus.FAILED.value,
                        "error": str(e),
                    }
                },
            ),
            error=str(e),
        )
        return

    # ---------- REPO GRAPH ----------
    _abort_if_requested(job_id, repo_name, IngestionStage.REPO_GRAPH)
    try:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.REPO_GRAPH,
                stage_status={IngestionStage.REPO_GRAPH: IngestionStageStatus.RUNNING},
            )
        )

        await CreateRepoGraphStage(job_id=job_id).run(
            local_path=local_repo_path,
            repo_id=repo_name,
            changed_files=changed_files,
        )

        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.REPO_GRAPH,
                stage_status={
                    IngestionStage.REPO_GRAPH: {
                        "status": IngestionStageStatus.COMPLETED.value
                    }
                },
            )
        )
    except JobAborted:
        return
    
    except Exception as e:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="failed",
                current_stage=IngestionStage.REPO_GRAPH,
                stage_status={
                    IngestionStage.REPO_GRAPH: {
                        "status": IngestionStageStatus.FAILED.value,
                        "error": str(e),
                    }
                },
            ),
            error=str(e),
        )
        return

    # ---------- MENTAL MODEL ----------
    _abort_if_requested(job_id, repo_name, IngestionStage.MENTAL_MODEL)
    try:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.MENTAL_MODEL,
                stage_status={IngestionStage.MENTAL_MODEL: IngestionStageStatus.RUNNING},
            )
        )

        critical_file_count, ignored_files_count, repo_context_token_count = await MentalModelStage(llm_grok=llm, config={"job_id": job_id}).run(
            repo_id=repo_name,
            local_repo_path=local_repo_path,
        )

        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="completed",
                current_stage=IngestionStage.MENTAL_MODEL,
                stage_status={
                    IngestionStage.MENTAL_MODEL: {
                        "status": IngestionStageStatus.COMPLETED.value,
                        "metrics": {
                            "critical_files": critical_file_count,
                            "files_ignored": ignored_files_count,
                            "repo_context_token_count": repo_context_token_count,
                        },
                    }
                },
            )
        )
    except JobAborted:
        return
    
    except Exception as e:
        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="failed",
                current_stage=IngestionStage.MENTAL_MODEL,
                stage_status={
                    IngestionStage.MENTAL_MODEL: {
                        "status": IngestionStageStatus.FAILED.value,
                        "error": str(e),
                    }
                },
            ),
            error=str(e),
        )
        return

    # ---------- COMPLETION ----------
    mongo.add_ingested_repo(repo_name=repo_name, job_id=job_id)

    return {"status": "completed", "job_id": job_id}


