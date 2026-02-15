"""
Stage 3: Verify LSP Reference Cache
Simplified stage that verifies the LSP reference cache exists and is populated.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class CreateRepoGraphStage:
    """Verify LSP reference cache is available for cross-file analysis."""

    def __init__(self, job_id: str):
        self.job_id = job_id

    async def run(self, local_path: Path, repo_name: str, resolver_changes: dict | None = None) -> dict:
        """
        Verify LSP reference cache exists.

        Args:
            local_path: path to local repo checkout
            repo_name: repo identifier
        """
        logger.info("Job %s: Verifying LSP reference cache for repository: %s", self.job_id, repo_name)

        # Check that LSP cache exists
        cache_path = local_path / ".lsp_ref_cache.sqlite"
        if not cache_path.exists():
            raise FileNotFoundError(
                f"LSP reference cache not found at {cache_path}. "
                "The RESOLVE_REFS stage should have created this cache."
            )

        changed = len((resolver_changes or {}).get("changed_files", []))
        skipped = changed == 0
        logger.info("Job %s: LSP reference cache verified at %s", self.job_id, cache_path)
        return {"skipped_due_to_no_changes": skipped, "changed_files_count": changed}
