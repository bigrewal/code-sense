import asyncio
from pathlib import Path
from dataclasses import asdict
from typing import Optional
from uuid import uuid4

from ..models.data_model import IngestionJobStatus, IngestionStage, IngestionStageStatus
from ..db import get_db_client
from ..llm_grok import GrokLLM
from .file_state import build_repo_file_changes
from .mental_model_gen import MentalModelStage
from .pre_ingestion_analysis import PreIngestionAnalysisError, PreIngestionAnalysisStage

async def start_ingestion_pipeline(
    local_repo_path: Path,
    repo_name: str,
    job_id: Optional[str] = None,
) -> dict[str, str]:
    try:
        db_client = get_db_client()
        llm = GrokLLM()

        job_id = job_id or str(uuid4())

        db_client.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.PRECHECK,
                stage_status={
                    IngestionStage.PRECHECK: IngestionStageStatus.PENDING,
                    IngestionStage.MENTAL_MODEL: IngestionStageStatus.PENDING,
                },
            )
        )

        previous_state = db_client.get_repo_file_states(repo_name)
        file_changes_obj = build_repo_file_changes(local_repo_path, previous_state)
        file_changes = asdict(file_changes_obj)

        # ---------- PRECHECK ----------
        try:
            db_client.upsert_ingestion_job(
                IngestionJobStatus(
                    job_id=job_id,
                    repo_name=repo_name,
                    status="running",
                    current_stage=IngestionStage.PRECHECK,
                    stage_status={IngestionStage.PRECHECK: IngestionStageStatus.RUNNING},
                )
            )

            pre_ingestion_stage = PreIngestionAnalysisStage(
                llm_grok=llm,
                repo_name=repo_name,
            )
            analysis_summary = await pre_ingestion_stage.run(
                repo_path=local_repo_path,
                file_changes=file_changes_obj,
                previous_state=previous_state,
            )
            analysis_summary.update(
                {
                    "new_files": len(file_changes["new_files"]),
                    "changed_files": len(file_changes["changed_files"]),
                    "deleted_files": len(file_changes["deleted_files"]),
                    "unchanged_files": len(file_changes["unchanged_files"]),
                }
            )

            db_client.upsert_ingestion_job(
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
            db_client.upsert_ingestion_job(
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
            db_client.upsert_ingestion_job(
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

        # ---------- MENTAL MODEL ----------
        try:
            db_client.upsert_ingestion_job(
                IngestionJobStatus(
                    job_id=job_id,
                    repo_name=repo_name,
                    status="running",
                    current_stage=IngestionStage.MENTAL_MODEL,
                    stage_status={IngestionStage.MENTAL_MODEL: IngestionStageStatus.RUNNING},
                )
            )

            critical_file_count, ignored_files_count, repo_context_token_count = await MentalModelStage(
                llm_grok=llm,
                config={"job_id": job_id},
            ).run(
                repo_name=repo_name,
                local_repo_path=local_repo_path,
                file_changes=file_changes,
            )

            db_client.upsert_ingestion_job(
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
        except Exception as e:
            db_client.upsert_ingestion_job(
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
        db_client.add_ingested_repo(repo_name=repo_name, job_id=job_id)

        return {"status": "completed", "job_id": job_id}

    except asyncio.CancelledError:
        db_client.cancel_active_ingestion_jobs(f"Ingestion cancelled: job {job_id} interrupted")
        raise
