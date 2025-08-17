"""
Stage 3: Reference Resolution
Uses tree-sitter-reference-resolver to analyze code references.
"""

import time
from typing import Any
from pathlib import Path

from .core.base import PipelineStage, StageResult
from ..models.data_model import ReferenceResolutionResult, S3StorageInfo
import tree_sitter_reference_resolver as tsrr


class ReferenceResolverStage(PipelineStage):
    """Stage for resolving code references using tree-sitter."""
    
    def __init__(self, config: dict = None):
        super().__init__("Reference Resolution", config)
        self.include_unresolved = config.get("include_unresolved", True)
        self.reuse_database = config.get("reuse_database", False)
        self.pipeline_id = config.get("pipeline_id", "unknown")
    
    async def execute(self, input_data: S3StorageInfo) -> StageResult:
        """
        Resolve references in the downloaded repository.
        
        Args:
            input_data: S3StorageInfo with local repository path
            
        Returns:
            StageResult with ReferenceResolutionResult data
        """
        try:
            repo_path = input_data.local_path
            print(f"Job {self.pipeline_id}: Starting reference resolution for: {repo_path}")
            
            # Configure the resolver
            config = tsrr.ResolverConfig(
                include_unresolved=self.include_unresolved,
                reuse_database=self.reuse_database,
                db_filename = f"stack-graphs-{input_data.repo_info.repo_id.split('/')[-1]}.db"

            )
            
            # Create resolver and process repository
            start_time = time.time()
            resolver = tsrr.PythonReferenceResolver(config)
            references = resolver.resolve_references(str(repo_path))
            processing_time = time.time() - start_time
            
            # Calculate statistics
            resolved_count = sum(1 for ref in references if ref.has_definitions())
            unresolved_count = len(references) - resolved_count

            unique_references = set()
            unique_ref_to_defs = {}
            repo_path_absolute = repo_path.resolve()

            for ref_to_def in references:
                if ref_to_def.has_definitions():
                    ref_info = ref_to_def.reference
                    
                    abs_ref_path  = str(repo_path / Path(ref_info.file_path).relative_to(repo_path_absolute))
                    ref_key = (abs_ref_path, ref_info.line_number, ref_info.column)
                    if ref_key not in unique_references:
                        unique_references.add(ref_key)
                        # print(f"Job {self.pipeline_id}: Found reference: {ref_key}")
                        defs = []
                        for definition in ref_to_def.definitions:
                            abs_def_path  = str(repo_path / Path(definition.file_path).relative_to(repo_path_absolute))
                            def_key = (abs_def_path, definition.line_number, definition.column)
                            defs.append(def_key)
                            
                            # print(
                            #     f"Job {self.pipeline_id}: Resolved definition: {def_key} ")

                        unique_ref_to_defs[ref_key] = defs

            result = ReferenceResolutionResult(
                repo_path=repo_path,
                references=unique_ref_to_defs,
                total_references=len(references),
                resolved_count=resolved_count,
                unresolved_count=unresolved_count,
                processing_time=processing_time
            )
            
            print(
                f"Job {self.pipeline_id}: Reference resolution completed: "
                f"{len(references)} total, {resolved_count} resolved, "
                f"{unresolved_count} unresolved in {processing_time:.2f}s"
            )
            
            return StageResult(
                success=True,
                data={
                    "s3_info": input_data,
                    "reference_results": result
                },
                metadata={
                    "stage": self.name,
                    "total_references": len(references),
                    "resolved_count": resolved_count,
                    "unresolved_count": unresolved_count,
                    "processing_time": processing_time
                }
            )
            
        except Exception as e:
            print(f"Job {self.pipeline_id}: Reference resolution error: {str(e)}")
            return StageResult(
                success=False,
                data=None,
                metadata={"stage": self.name},
                error=str(e)
            )
    
    def validate_config(self) -> bool:
        """Validate stage configuration."""
        return True  # All config options have defaults