
from dataclasses import dataclass
from enum import Enum
from typing import Any


class IngestionStage(str, Enum):
    QUEUED = "queued"
    PRECHECK = "precheck"
    RESOLVE_REFS = "resolve_refs"
    REPO_GRAPH = "repo_graph"
    MENTAL_MODEL = "mental_model"

class IngestionStageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class IngestionJobStatus:
    job_id: str
    repo_name: str
    status: str
    current_stage: IngestionStage
    stage_status: dict[IngestionStage, Any]


class JobAborted(Exception):
    pass
