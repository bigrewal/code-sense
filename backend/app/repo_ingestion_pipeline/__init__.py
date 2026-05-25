import asyncio
import logging
from pathlib import Path
from dataclasses import asdict
from uuid import uuid4

from ..models.data_model import IngestionJobStatus, IngestionStage, IngestionStageStatus
from ..db import get_db_client
from ..llm import get_llm_provider
from .file_state import build_repo_file_changes
from .mental_model_gen import MentalModelStage
from .pre_ingestion_analysis import PreIngestionAnalysisError, PreIngestionAnalysisStage

logger = logging.getLogger(__name__)


def _invalidate_chat_repo_context_cache(repo_name: str) -> None:
    try:
        from ..chat_service import invalidate_repo_context_cache
    except Exception:
        return
    try:
        invalidate_repo_context_cache(repo_name)
    except Exception:
        logger.exception("Failed to invalidate repo context cache for %s", repo_name)


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
    job_id = job_id or str(uuid4())
    db_client = None
    reached_terminal_state = False
    current_stage = IngestionStage.PRECHECK

    try:
        db_client = get_db_client()
        llm = get_llm_provider()

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
            reached_terminal_state = True
            return
        except Exception as e:
            _save_job(
                db_client, job_id, repo_name, "failed", IngestionStage.PRECHECK,
                {IngestionStage.PRECHECK: _payload(IngestionStageStatus.FAILED, error=str(e))},
                error=str(e),
            )
            reached_terminal_state = True
            raise

        # ---------- MENTAL MODEL ----------
        current_stage = IngestionStage.MENTAL_MODEL
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
            _invalidate_chat_repo_context_cache(repo_name)
        except Exception as e:
            _save_job(
                db_client, job_id, repo_name, "failed", IngestionStage.MENTAL_MODEL,
                {IngestionStage.MENTAL_MODEL: _payload(IngestionStageStatus.FAILED, error=str(e))},
                error=str(e),
            )
            reached_terminal_state = True
            return

        # ---------- COMPLETION ----------
        db_client.add_ingested_repo(repo_name=repo_name, job_id=job_id, local_path=str(local_repo_path.resolve()))
        reached_terminal_state = True
        return {"status": "completed", "job_id": job_id}

    except asyncio.CancelledError:
        if db_client is not None:
            db_client.cancel_active_ingestion_jobs(f"Ingestion cancelled: job {job_id} interrupted")
            reached_terminal_state = True
        raise
    finally:
        if not reached_terminal_state and db_client is not None:
            try:
                error_msg = f"Ingestion job {job_id} aborted before completion"
                _save_job(
                    db_client, job_id, repo_name, "failed", current_stage,
                    {current_stage: _payload(IngestionStageStatus.FAILED, error=error_msg)},
                    error=error_msg,
                )
            except Exception:
                logger.exception("Failed to mark job %s as failed during cleanup", job_id)
