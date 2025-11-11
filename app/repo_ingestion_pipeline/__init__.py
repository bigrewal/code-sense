from typing import Any, Dict
from ..config import Config
from ..models.data_model import ReferenceResolutionResult
from .resolve_references import ReferenceResolver
from .create_repo_graph import ASTProcessorStage
from .mental_model_gen import MentalModelStage
from pathlib import Path


async def start_ingestion_pipeline(local_repo_path: Path, repo_name: str, job_id: str) -> any:
    # pipeline = Pipeline(config)

    config = Config()
    # resolved_refs: Dict[str, Any] = await ReferenceResolver({"job_id": job_id, **config.RESOLVER_CONFIG}).run(repo_path=local_repo_path, repo_id=repo_name)
    # # print(f"Resolved References")
    # await ASTProcessorStage({"job_id": job_id, **config.AST_CONFIG}).run(local_path=local_repo_path, repo_id=repo_name, reference_results=resolved_refs)
    await MentalModelStage({"job_id": job_id}).run(repo_id=repo_name, local_repo_path=local_repo_path)