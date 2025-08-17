from ..config import Config
from ..models.data_model import UploadedRepository, S3StorageInfo
from .core.pipeline import Pipeline, PipelineConfig
from .resolve_references import ReferenceResolverStage
from .code_reader import ChunkLLMProcessorStage
from .create_repo_graph import ASTProcessorStage
from .mental_model_gen import MentalModelStage
from pathlib import Path


async def start_ingestion_pipeline(local_repo_path: Path, job_id: str = None) -> any:
    # local_repo_path = Path("data") / repo_path
    
    # if not local_repo_path.exists():
    #     print(f"❌ Repository not found: {local_repo_path}")
    #     return
    
    # print(f"🔍 Job {job_id}: Testing pipeline with: {local_repo_path}")
    
    # Create fake S3 storage info (what the resolver stage expects)
    
    repo_info = UploadedRepository(
        repo_id=str(local_repo_path),
        original_filename="test-repo",
        file_size=1000,
        upload_timestamp="2024-01-01T00:00:00"
    )
    
    s3_info = S3StorageInfo(
        bucket_name="test-bucket",
        s3_key="test-key",
        local_path=local_repo_path,
        repo_info=repo_info
    )
    
    # Create pipeline with only resolver and AST stages
    config = PipelineConfig(
        pipeline_id=job_id,
        stages_config={
            "resolver": Config.RESOLVER_CONFIG,
            "ast": Config.AST_CONFIG
        }
    )
    
    pipeline = Pipeline(config)

    config = Config()
    resolver_stage = ReferenceResolverStage({"job_id": job_id, **config.RESOLVER_CONFIG})
    ast_stage = ASTProcessorStage({"job_id": job_id, **config.AST_CONFIG})
    llm_stage = ChunkLLMProcessorStage({"job_id": job_id})
    mental_model_stage = MentalModelStage({"job_id": job_id})  # Reuse LLM config or add specific

    pipeline.add_stages([resolver_stage, ast_stage, llm_stage, mental_model_stage])
    
    print(f"Job {job_id}: Starting pipeline execution...")
    
    # Run the pipeline
    result = await pipeline.execute(s3_info)
    
    if result.success:
        print(f"Job {job_id}: ✅ Pipeline completed successfully!")
        for i, stage_result in enumerate(result.stage_results):
            print(f"Job {job_id}: 📊 Stage {i+1}: {stage_result.metadata}")
    else:
        print(f"Job {job_id}: ❌ Pipeline failed: {result.error}")