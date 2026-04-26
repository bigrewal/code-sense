import asyncio
from pathlib import Path
from dataclasses import asdict
from uuid import uuid4

from ..models.data_model import IngestionJobStatus, IngestionStage, IngestionStageStatus
from ..db import get_db_client
from ..llm_grok import GrokLLM
from .file_state import build_repo_file_changes
from .mental_model_gen import MentalModelStage
from .pre_ingestion_analysis import PreIngestionAnalysisError, PreIngestionAnalysisStage


def _save_job(db_client, job_id, repo_name, status, stage, stage_status, **kwargs):
    db_client.upsert_ingestion_job(
        IngestionJobStatus(
            job_id=job_id,
            repo_name=repo_name,
            status=status,
            current_stage=stage,
            stage_status=stage_status,
        ),
        **kwargs,
    )


def _payload(status, **extra):
    return {"status": status.value, **extra}


async def start_ingestion_pipeline(
    local_repo_path: Path,
    repo_name: str,
    job_id: str | None = None,
) -> dict[str, str]:
    try:
        db_client = get_db_client()
        llm = GrokLLM()

        job_id = job_id or str(uuid4())

        _save_job(
            db_client, job_id, repo_name, "running", IngestionStage.PRECHECK,
            {
                IngestionStage.PRECHECK: IngestionStageStatus.PENDING,
                IngestionStage.MENTAL_MODEL: IngestionStageStatus.PENDING,
            },
        )

        previous_state = db_client.get_repo_file_states(repo_name)
        file_changes_obj = build_repo_file_changes(local_repo_path, previous_state)
        file_changes = asdict(file_changes_obj)

        # ---------- PRECHECK ----------
        try:
            _save_job(
                db_client, job_id, repo_name, "running", IngestionStage.PRECHECK,
                {IngestionStage.PRECHECK: IngestionStageStatus.RUNNING},
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

            _save_job(
                db_client, job_id, repo_name, "running", IngestionStage.PRECHECK,
                {IngestionStage.PRECHECK: _payload(IngestionStageStatus.COMPLETED, metrics=analysis_summary)},
            )

        except PreIngestionAnalysisError as pie:
            _save_job(
                db_client, job_id, repo_name, "failed", IngestionStage.PRECHECK,
                {IngestionStage.PRECHECK: _payload(IngestionStageStatus.FAILED, error=str(pie))},
                error=str(pie),
            )
            return
        except Exception as e:
            _save_job(
                db_client, job_id, repo_name, "failed", IngestionStage.PRECHECK,
                {IngestionStage.PRECHECK: _payload(IngestionStageStatus.FAILED, error=str(e))},
                error=str(e),
            )
            raise

        # ---------- MENTAL MODEL ----------
        try:
            _save_job(
                db_client, job_id, repo_name, "running", IngestionStage.MENTAL_MODEL,
                {IngestionStage.MENTAL_MODEL: IngestionStageStatus.RUNNING},
            )

            critical_file_count, ignored_files_count, repo_context_token_count = await MentalModelStage(
                llm_grok=llm,
                config={"job_id": job_id},
            ).run(
                repo_name=repo_name,
                local_repo_path=local_repo_path,
                file_changes=file_changes,
            )

            _save_job(
                db_client, job_id, repo_name, "completed", IngestionStage.MENTAL_MODEL,
                {
                    IngestionStage.MENTAL_MODEL: _payload(
                        IngestionStageStatus.COMPLETED,
                        metrics={
                            "critical_files": critical_file_count,
                            "files_ignored": ignored_files_count,
                            "repo_context_token_count": repo_context_token_count,
                        },
                    )
                },
            )
        except Exception as e:
            _save_job(
                db_client, job_id, repo_name, "failed", IngestionStage.MENTAL_MODEL,
                {IngestionStage.MENTAL_MODEL: _payload(IngestionStageStatus.FAILED, error=str(e))},
                error=str(e),
            )
            return

        # ---------- COMPLETION ----------
        db_client.add_ingested_repo(repo_name=repo_name, job_id=job_id, local_path=str(local_repo_path.resolve()))

        return {"status": "completed", "job_id": job_id}

    except asyncio.CancelledError:
        db_client.cancel_active_ingestion_jobs(f"Ingestion cancelled: job {job_id} interrupted")
        raise
