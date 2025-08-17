"""
Base stage interface for the pipeline.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from dataclasses import dataclass
import time
from loguru import logger


@dataclass
class StageResult:
    """Result from a pipeline stage."""
    success: bool
    data: Any
    metadata: Dict[str, Any]
    error: Optional[str] = None
    execution_time: Optional[float] = None


class PipelineStage(ABC):
    """Base class for all pipeline stages."""
    
    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.config = config or {}
        
    @abstractmethod
    async def execute(self, input_data: Any) -> StageResult:
        """
        Execute the stage with the given input data.
        
        Args:
            input_data: Data from the previous stage or initial input
            
        Returns:
            StageResult containing the output data and metadata
        """
        pass
    
    async def run(self, input_data: Any) -> StageResult:
        """
        Run the stage with timing and error handling.
        """
        logger.info(f"Starting stage: {self.name}")
        start_time = time.time()
        
        try:
            result = await self.execute(input_data)
            execution_time = time.time() - start_time
            result.execution_time = execution_time
            
            logger.info(f"Stage {self.name} completed in {execution_time:.2f}s")
            return result
            
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"Stage {self.name} failed after {execution_time:.2f}s: {str(e)}")
            
            return StageResult(
                success=False,
                data=None,
                metadata={"stage": self.name},
                error=str(e),
                execution_time=execution_time
            )
    
    def validate_config(self) -> bool:
        """Validate stage configuration. Override in subclasses."""
        return True