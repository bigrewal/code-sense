import asyncio
import hashlib
import logging
from pathlib import Path
import traceback
from typing import Dict, List, Optional, Set, Tuple

from tqdm import tqdm

from ..db import LSPCacheReader, get_mongo_client
from ..llm_grok import GrokLLM
from ..contextual_retrieval import ContextualRetrieval
from .file_state import list_contextual_retrieval_candidates

logger = logging.getLogger(__name__)


MENTAL_MODEL_TYPES = {
    "BRIEF": "BRIEF_FILE_OVERVIEW",
    "IGNORED": "IGNORED_FILE",
    "ENTRY": "POTENTIAL_ENTRY_POINTS",
}

PROMPT_SYSTEM = (
   "You analyze one repository file at a time.\n\n"
    "Step 1: classify file criticality.\n"
    "A file is CRITICAL if it directly implements/coordinates core product behavior, API surface, core data model, orchestration, or essential integration flow.\n"
    "A file is NOT critical if it is primarily tests, fixtures, docs, examples, demos, generated code, config-only, or thin wrappers with no meaningful domain behavior.\n\n"
    "If NOT critical, output exactly: IGNORE\n\n"
    "If the file is critical, output exactly a concise summary using the required sentence template.\n"
    "Be factual and concise. Use only evidence from code and dependency context.\n"
    "No bullets, no markdown, no extra commentary."
)

PROMPT_USER_TEMPLATE = """
    Repository: {repo_name}
    File: {file_path}

    Code:
    {code}

    Upstream dependencies (who calls/uses this file):
    {upstream}

    Downstream dependencies (what this file calls/uses):
    {downstream}

    Instructions:
    - If NOT critical, output exactly: IGNORE
    - If CRITICAL, output one concise paragraph in this exact 3-sentence format:

    "`{file_path}` <what this file does and why it exists>. It does this by <main components/functions/classes and the core flow>. It interacts upstream with <key callers/modules> and downstream with <key callees/services/files>."

    Rules:
    - Keep it very concise: 50-100 words total.
    - Mention concrete identifiers when available.
    - No bullets, no markdown, no extra text.
    """.strip()


class MentalModelStage:
    """Stage for generating and storing the hierarchical mental model."""

    def __init__(self, llm_grok: GrokLLM, config: Optional[dict] = None):
        config = config or {}
        self.mongo_client = get_mongo_client()
        self.mental_model_collection = self.mongo_client["mental_model"]
        self.llm_client = llm_grok
        self.job_id = config.get("job_id", "unknown")
        self.batch_size = int(config.get("batch_size", 20))
        self.max_concurrency = int(config.get("max_concurrency", 10))

    async def run(
        self,
        repo_name: str,
        local_repo_path: Path,
        file_changes: Optional[dict] = None,
        resolver_changes: Optional[dict] = None,
        retrieval_enabled: bool = True,
    ):
        self.lsp_cache = LSPCacheReader(str(local_repo_path))
        self.repo_name = repo_name
        self.local_repo_path = local_repo_path
        logger.info("Job %s: starting mental model generation for %s", self.job_id, repo_name)

        try:
            dir_tree = self._build_dir_tree()
            files_to_process = self._select_files_to_process(
                repo_name=repo_name,
                all_files=dir_tree,
                file_changes=file_changes,
                resolver_changes=resolver_changes,
            )
            deleted_files = sorted((file_changes or {}).get("deleted_files", []))
            await self._delete_removed_artifacts(repo_name, deleted_files, retrieval_enabled=retrieval_enabled)

            critical_files, ignored_files = await self.identify_critical_files(
                files_to_process, repo_name, retrieval_enabled=retrieval_enabled
            )
            logger.info(
                "Job %s: generated overview from %s files with %s critical files, ignored %s files",
                self.job_id,
                len(files_to_process),
                len(critical_files),
                len(ignored_files),
            )
            repo_context_token_count = await self.create_repo_context(repo_name)
            critical_total, ignored_total = self._get_repo_file_classification_counts(repo_name)

            return critical_total, ignored_total, repo_context_token_count

        except Exception as e:
            logger.exception("Job %s: mental model generation error", self.job_id)
            traceback.print_exc()
            raise e

    async def identify_critical_files(
        self,
        dir_tree: List[str],
        repo_name: str,
        retrieval_enabled: bool = True,
    ) -> Tuple[List[Dict[str, str]], Set[str]]:
        """Generate a comprehensive overview of the repo by summarizing critical files, ignoring non-critical ones."""

        semaphore = asyncio.Semaphore(self.max_concurrency)
        retrieval = ContextualRetrieval(repo_name) if retrieval_enabled else None

        async def summarize_file(file_path: str) -> tuple[str, str, str | None, str | None]:
            try:
                full_path = self.local_repo_path / file_path
                code_bytes = full_path.read_bytes()
                code = code_bytes.decode("utf-8")
                sha1 = hashlib.sha1(code_bytes).hexdigest()
            except Exception:
                logger.warning("Job %s: unable to read %s; marking ignored", self.job_id, file_path)
                return file_path, "IGNORE", None, None

            cached = self.mental_model_collection.find_one(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "document_type": {"$in": [MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]]},
                    "sha1": sha1,
                },
                {"_id": 0, "data": 1},
            )
            if cached:
                return file_path, cached["data"], code, sha1

            cfi = self.lsp_cache.cross_file_interactions(file_path)
            downstream_info = cfi.get("downstream", {}) or {}
            upstream_info = cfi.get("upstream", {}) or {}

            downstream_interactions: list[str] = []
            for dep_file, interactions in (downstream_info.get("interactions", {}) or {}).items():
                for inter in interactions or []:
                    downstream_interactions.append(f"{file_path} → {dep_file}: {inter}")

            upstream_interactions: list[str] = []
            for src_file, interactions in (upstream_info.get("interactions", {}) or {}).items():
                for inter in interactions or []:
                    upstream_interactions.append(f"{src_file} → {file_path}: {inter}")

            upstream_block = "\n".join(f"- {i}" for i in (upstream_interactions or [])) or "—"
            downstream_block = "\n".join(f"- {i}" for i in (downstream_interactions or [])) or "—"

            user_prompt = PROMPT_USER_TEMPLATE.format(
                repo_name=repo_name,
                file_path=file_path,
                code=code,
                upstream=upstream_block,
                downstream=downstream_block,
            )

            async with semaphore:
                response = await self.llm_client.generate_async(
                    prompt=user_prompt,
                    system_prompt=PROMPT_SYSTEM,
                    temperature=0.0,
                )
            return file_path, response.strip(), code, sha1

        all_files = dir_tree

        insights: List[Dict[str, str]] = []
        ignored: Set[str] = set()

        pbar = tqdm(total=len(all_files), desc="Processing files")

        for i in range(0, len(all_files), self.batch_size):
            batch = all_files[i : i + self.batch_size]
            tasks = [summarize_file(fp) for fp in batch]
            results = await asyncio.gather(*tasks)

            for fp, summary, _code, sha1 in results:
                if summary == "IGNORE":
                    ignored.add(fp)
                    if sha1:
                        self._replace_file_document(repo_name, fp, MENTAL_MODEL_TYPES["IGNORED"], "IGNORE", sha1)
                else:
                    insights.append({"file_path": fp, "summary": summary})
                    if sha1:
                        self._replace_file_document(repo_name, fp, MENTAL_MODEL_TYPES["BRIEF"], summary, sha1)

            pbar.update(len(batch))

        pbar.close()

        # Index files for contextual retrieval in parallel
        if retrieval_enabled and retrieval is not None:
            retrieval_candidates = list_contextual_retrieval_candidates(self.local_repo_path)
            retrieval_pbar = tqdm(total=len(retrieval_candidates), desc="Contextual retrieval")
            for fp in retrieval_candidates:
                try:
                    code = (self.local_repo_path / fp).read_bytes().decode("utf-8")
                    retrieval.delete_file(fp)
                    await retrieval.index_file(fp, code)
                except Exception as e:
                    logger.warning("Job %s: failed to index %s: %s", self.job_id, fp, e)
                finally:
                    retrieval_pbar.update(1)
            retrieval_pbar.close()

        for insight in insights:
            file_path = insight["file_path"]
            dependency_info = self.lsp_cache.cross_file_interactions(file_path)
            insight["downstream_dep_interactions"] = dependency_info["downstream"]["interactions"]
            insight["downstream_dep_files"] = list(dependency_info["downstream"]["files"])
            insight["upstream_dep_interactions"] = dependency_info["upstream"]["interactions"]
            insight["upstream_dep_files"] = list(dependency_info["upstream"]["files"])

        return insights, ignored

    async def create_repo_context(self, repo_name: str) -> int:
        critical_files = set(self.mongo_client.get_critical_file_paths(repo_name))
        context_parts: list[str] = []

        for file_path in critical_files:
            brief = self.mongo_client.get_brief_file_overview(repo_name, file_path)
            if brief:
                context_parts.append(brief)

        repo_context = "\n\n".join(context_parts)
        repo_context_token_count = self.llm_client.count_tokens(repo_context)

        doc = {
            "repo_name": repo_name,
            "document_type": "REPO_CONTEXT",
            "context": repo_context,
        }
        self.mental_model_collection.update_one(
            {
                "repo_name": repo_name,
                "document_type": "REPO_CONTEXT",
            },
            {"$set": doc},
            upsert=True,
        )

        return repo_context_token_count

    def _get_repo_file_classification_counts(self, repo_name: str) -> tuple[int, int]:
        critical_total = self.mental_model_collection.count_documents(
            {"repo_name": repo_name, "document_type": MENTAL_MODEL_TYPES["BRIEF"]}
        )
        ignored_total = self.mental_model_collection.count_documents(
            {"repo_name": repo_name, "document_type": MENTAL_MODEL_TYPES["IGNORED"]}
        )
        return critical_total, ignored_total

    def _build_dir_tree(self) -> List[str]:
        """Build a list of code file paths by walking the repository."""
        import os
        from ..config import Config

        code_files = []
        supported_extensions = set(Config.SUPPORTED_LANGUAGES.keys())
        ignore_folders = Config.IGNORE_FOLDERS

        for root, dirs, files in os.walk(self.local_repo_path):
            root_path = Path(root)

            # Filter out ignored directories in-place
            dirs[:] = [d for d in dirs if d not in ignore_folders and not d.startswith('.')]

            for file in files:
                file_path = root_path / file
                if file_path.suffix in supported_extensions:
                    # Return path relative to repo root for consistency with LSP cache
                    relative_path = file_path.relative_to(self.local_repo_path)
                    code_files.append(str(relative_path))

        return sorted(code_files)

    def _upsert_document(self, document: Dict):
        """Persist a mental model document via upsert."""
        self.mental_model_collection.update_one(
            {
                "repo_name": document["repo_name"],
                "file_path": document["file_path"],
                "document_type": document["document_type"],
            },
            {"$set": document},
            upsert=True,
        )

    def _replace_file_document(self, repo_name: str, file_path: str, doc_type: str, data: str, sha1: str):
        self.mental_model_collection.delete_many(
            {
                "repo_name": repo_name,
                "file_path": file_path,
                "document_type": {"$in": [MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]]},
            }
        )
        self._upsert_document(
            {
                "repo_name": repo_name,
                "file_path": file_path,
                "document_type": doc_type,
                "data": data,
                "sha1": sha1,
            }
        )

    async def _delete_removed_artifacts(
        self,
        repo_name: str,
        deleted_files: List[str],
        retrieval_enabled: bool = True,
    ):
        if not deleted_files:
            return
        self.mental_model_collection.delete_many(
            {
                "repo_name": repo_name,
                "file_path": {"$in": deleted_files},
                "document_type": {"$in": [MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]]},
            }
        )
        if retrieval_enabled:
            retrieval = ContextualRetrieval(repo_name)
            retrieval.delete_files(deleted_files)

    def _select_files_to_process(
        self,
        repo_name: str,
        all_files: List[str],
        file_changes: Optional[dict],
        resolver_changes: Optional[dict],
    ) -> List[str]:
        if not file_changes and not resolver_changes:
            return all_files

        selected: Set[str] = set()
        selected.update(file_changes.get("new_files", []) if file_changes else [])
        selected.update(file_changes.get("changed_files", []) if file_changes else [])

        prefix = f"{repo_name}/"
        for path in (resolver_changes or {}).get("impacted_ref_files", []):
            selected.add(path[len(prefix):] if path.startswith(prefix) else path)

        # Backfill missing mental-model docs even when there are no file deltas.
        existing = self.mental_model_collection.find(
            {
                "repo_name": repo_name,
                "document_type": {"$in": [MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]]},
            },
            {"_id": 0, "file_path": 1},
        )
        documented_files = {doc.get("file_path") for doc in existing if doc.get("file_path")}
        for path in all_files:
            if path not in documented_files:
                selected.add(path)

        allowed = set(all_files)
        return sorted([p for p in selected if p in allowed])
