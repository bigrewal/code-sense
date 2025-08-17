"""
Pipeline orchestrator that manages the execution of stages.
"""

from typing import List, Any, Dict, Optional
from dataclasses import dataclass
import asyncio
from loguru import logger
from datetime import datetime

from .base import PipelineStage, StageResult
from app.db import get_mongo_client


@dataclass
class PipelineConfig:
    """Configuration for the entire pipeline."""
    pipeline_id: str
    stages_config: Dict[str, Dict[str, Any]]
    continue_on_failure: bool = False
    max_retries: int = 0
    retry_delay: float = 1.0


@dataclass
class PipelineResult:
    """Result from the entire pipeline execution."""
    success: bool
    pipeline_id: str
    stage_results: List[StageResult]
    total_execution_time: float
    error: Optional[str] = None


class Pipeline:
    """Pipeline orchestrator that executes stages sequentially."""
    
    def __init__(self, config: PipelineConfig):
        self.config = config
        self.mongo_db = get_mongo_client()
        self.job_status_collection = self.mongo_db["job_status"]
        self.job_status_collection.create_index([("job_id", 1), ("stage_name", 1)], unique=True)
        self.stages: List[PipelineStage] = []
        
    def add_stage(self, stage: PipelineStage) -> "Pipeline":
        """Add a stage to the pipeline."""
        self.stages.append(stage)
        return self
    
    def add_stages(self, stages: List[PipelineStage]) -> "Pipeline":
        """Add multiple stages to the pipeline."""
        self.stages.extend(stages)
        return self
    
    async def execute(self, initial_data: Any) -> PipelineResult:
        """
        Execute all stages in the pipeline sequentially.
        
        Args:
            initial_data: Initial input data for the first stage
            
        Returns:
            PipelineResult containing results from all stages
        """
        logger.info(f"Starting pipeline: {self.config.pipeline_id}")
        start_time = asyncio.get_event_loop().time()
        
        stage_results = []
        current_data = initial_data
        
        for i, stage in enumerate(self.stages):
            logger.info(f"Executing stage {i+1}/{len(self.stages)}: {stage.name}")
            
            # Execute stage with retry logic
            # Add the status of the stage to the job status collection
            stage_status = {
                "job_id": self.config.pipeline_id,
                "stage_name": stage.name,
                "status": "running",
                "start_time": datetime.now().isoformat(),
            }
            self.job_status_collection.replace_one(
                {"job_id": self.config.pipeline_id, "stage_name": stage.name},
                stage_status,
                upsert=True
            )
            result = await self._execute_stage_with_retry(stage, current_data)
            stage_results.append(result)
            
            if not result.success:
                # Update the stage as failed in the job status collection
                stage_status["status"] = "failed"
                stage_status["end_time"] = datetime.now().isoformat()
                self.job_status_collection.replace_one(
                    {"job_id": self.config.pipeline_id, "stage_name": stage.name},
                    stage_status,
                    upsert=True
                )

                if self.config.continue_on_failure:
                    logger.warning(f"Stage {stage.name} failed, but continuing pipeline")
                    continue
                else:
                    logger.error(f"Stage {stage.name} failed, stopping pipeline")
                    total_time = asyncio.get_event_loop().time() - start_time
                    return PipelineResult(
                        success=False,
                        pipeline_id=self.config.pipeline_id,
                        stage_results=stage_results,
                        total_execution_time=total_time,
                        error=f"Pipeline failed at stage: {stage.name}"
                    )
            else:
                logger.info(f"Stage {stage.name} completed successfully")
                # Update the job status collection with the completed stage
                stage_status["status"] = "completed"
                stage_status["end_time"] = datetime.now().isoformat()
                self.job_status_collection.replace_one(
                    {"job_id": self.config.pipeline_id, "stage_name": stage.name},
                    stage_status,
                    upsert=True
                )
            # Pass the output to the next stage
            current_data = result.data
        
        total_time = asyncio.get_event_loop().time() - start_time
        logger.info(f"Pipeline {self.config.pipeline_id} completed in {total_time:.2f}s")
        
        return PipelineResult(
            success=True,
            pipeline_id=self.config.pipeline_id,
            stage_results=stage_results,
            total_execution_time=total_time
        )
    
    async def _execute_stage_with_retry(
        self, 
        stage: PipelineStage, 
        data: Any
    ) -> StageResult:
        """Execute a stage with retry logic."""
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0:
                logger.info(f"Retrying stage {stage.name}, attempt {attempt + 1}")
                await asyncio.sleep(self.config.retry_delay)
            
            result = await stage.run(data)
            
            if result.success:
                return result
            
            if attempt < self.config.max_retries:
                logger.warning(f"Stage {stage.name} failed, retrying...")
            else:
                logger.error(f"Stage {stage.name} failed after {self.config.max_retries + 1} attempts")
        
        return result
    
    def validate(self) -> bool:
        """Validate the entire pipeline configuration."""
        if not self.stages:
            logger.error("Pipeline has no stages")
            return False
        
        for stage in self.stages:
            if not stage.validate_config():
                logger.error(f"Stage {stage.name} has invalid configuration")
                return False
        
        return True