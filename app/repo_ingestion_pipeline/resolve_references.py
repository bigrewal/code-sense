"""
Stage 3: Reference Resolution
Uses tree-sitter-reference-resolver to analyze code references.
"""

import time
from typing import Any, Dict
from pathlib import Path

from ..lsp_reference_resolver.run import CodeAnalyzer



class ReferenceResolver():
    """Stage for resolving code references using tree-sitter."""
    
    def __init__(self, config: dict = None):
        self.include_unresolved = config.get("include_unresolved", True)
        self.reuse_database = config.get("reuse_database", False)
        self.pipeline_id = config.get("pipeline_id", "unknown")

    async def run(self, repo_path: Path, repo_id: str) -> Dict[str, Any]:
        """
        Resolve references in the downloaded repository.
        
        Args:
            input_data: S3StorageInfo with local repository path

        Returns:
            A dictionary containing the results of the reference resolution.
        """
        return await CodeAnalyzer(repo_path, repo_id).analyze()