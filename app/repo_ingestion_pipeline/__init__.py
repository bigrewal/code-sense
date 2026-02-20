from pathlib import Path
from dataclasses import asdict
from typing import Dict, List, Optional
from uuid import uuid4

from ..models.data_model import JobAborted, IngestionJobStatus, IngestionStage, IngestionStageStatus
from ..db import get_mongo_client
from ..utils import now_ts
from ..llm_grok import GrokLLM
from ..lsp_reference_resolver.run import CodeAnalyzer
from .file_state import build_repo_file_changes
from .mental_model_gen import MentalModelStage
from .pre_ingestion_analysis import PreIngestionAnalysisError, PreIngestionAnalysisStage

mongo = get_mongo_client()


def _initial_current_stage(enable_precheck: bool, enable_resolve_refs: bool) -> IngestionStage:
    if enable_precheck:
        return IngestionStage.PRECHECK
    if enable_resolve_refs:
        return IngestionStage.RESOLVE_REFS
    return IngestionStage.REPO_GRAPH


def _mark_stage_skipped(
    *,
    job_id: str,
    repo_name: str,
    skipped_stage: IngestionStage,
    next_stage: IngestionStage,
) -> None:
    mongo.upsert_ingestion_job(
        IngestionJobStatus(
            job_id=job_id,
            repo_name=repo_name,
            status="running",
            current_stage=next_stage,
            stage_status={
                skipped_stage: {
                    "status": IngestionStageStatus.COMPLETED.value,
                    "metrics": {"skipped": True},
                }
            },
        )
    )


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


def _verify_lsp_ref_cache(local_repo_path: Path, resolver_changes: dict | None = None) -> dict:
    cache_path = local_repo_path / ".lsp_ref_cache.sqlite"
    if not cache_path.exists():
        raise FileNotFoundError(
            f"LSP reference cache not found at {cache_path}. "
            "The RESOLVE_REFS stage should have created this cache."
        )

    changed = len((resolver_changes or {}).get("changed_files", []))
    return {"skipped_due_to_no_changes": changed == 0, "changed_files_count": changed}


async def start_ingestion_pipeline(
    local_repo_path: Path,
    repo_name: str,
    job_id: Optional[str] = None,
    enable_precheck: bool = True,
    enable_resolve_refs: bool = True,
) -> dict[str, str]:
    llm = GrokLLM()

    job_id = job_id or str(uuid4())
    initial_stage = _initial_current_stage(enable_precheck, enable_resolve_refs)

    # Initialize canonical stages (no string stage names)
    mongo.upsert_ingestion_job(
        IngestionJobStatus(
            job_id=job_id,
            repo_name=repo_name,
            status="running",
            current_stage=initial_stage,
            stage_status={
                IngestionStage.PRECHECK: IngestionStageStatus.PENDING,
                IngestionStage.RESOLVE_REFS: IngestionStageStatus.PENDING,
                IngestionStage.REPO_GRAPH: IngestionStageStatus.PENDING,
                IngestionStage.MENTAL_MODEL: IngestionStageStatus.PENDING,
            },
        )
    )

    previous_state = mongo.get_repo_file_states(repo_name)
    file_changes_obj = build_repo_file_changes(local_repo_path, previous_state)
    file_changes = asdict(file_changes_obj)
    resolver_changes: Dict[str, List[str]] = {
        "changed_files": [],
        "content_changed_files": [],
        "impacted_ref_files": [],
        "deleted_files": [],
    }

    # ---------- PRECHECK ----------
    if enable_precheck:
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
                llm_grok=llm, repo_name=repo_name
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
    else:
        _mark_stage_skipped(
            job_id=job_id,
            repo_name=repo_name,
            skipped_stage=IngestionStage.PRECHECK,
            next_stage=IngestionStage.RESOLVE_REFS if enable_resolve_refs else IngestionStage.REPO_GRAPH,
        )

    # ---------- RESOLVE / ANALYZE ----------
    if enable_resolve_refs:
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

            resolver_changes = await CodeAnalyzer(
                repo=local_repo_path, repo_name=repo_name, job_id=job_id
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
                            "metrics": {
                                "changed_files": len(resolver_changes.get("changed_files", [])),
                                "content_changed_files": len(resolver_changes.get("content_changed_files", [])),
                                "impacted_ref_files": len(resolver_changes.get("impacted_ref_files", [])),
                                "deleted_files": len(resolver_changes.get("deleted_files", [])),
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
    else:
        _mark_stage_skipped(
            job_id=job_id,
            repo_name=repo_name,
            skipped_stage=IngestionStage.RESOLVE_REFS,
            next_stage=IngestionStage.REPO_GRAPH,
        )

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

        repo_graph_metrics = _verify_lsp_ref_cache(
            local_repo_path=local_repo_path,
            resolver_changes=resolver_changes,
        )

        mongo.upsert_ingestion_job(
            IngestionJobStatus(
                job_id=job_id,
                repo_name=repo_name,
                status="running",
                current_stage=IngestionStage.REPO_GRAPH,
                stage_status={
                    IngestionStage.REPO_GRAPH: {
                        "status": IngestionStageStatus.COMPLETED.value,
                        "metrics": repo_graph_metrics,
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
            repo_name=repo_name,
            local_repo_path=local_repo_path,
            file_changes=file_changes,
            resolver_changes=resolver_changes,
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
