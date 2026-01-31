import asyncio
import logging
from pathlib import Path
import traceback
from typing import Dict, List, Optional, Set, Tuple

from tqdm import tqdm

from ..db import LSPCacheReader, get_mongo_client
from ..llm_grok import GrokLLM
from ..contextual_retrieval import ContextualRetrieval

logger = logging.getLogger(__name__)


MENTAL_MODEL_TYPES = {
    "BRIEF": "BRIEF_FILE_OVERVIEW",
    "IGNORED": "IGNORED_FILE",
    "ENTRY": "POTENTIAL_ENTRY_POINTS",
}

PROMPT_SYSTEM = (
    "You are analyzing a single code file from a repository.\n\n"
    "First, decide whether this file is CRITICAL to the repository’s core functionality.\n\n"
    "A file is CRITICAL if it directly implements, coordinates, or enables the primary behavior "
    "of the system (e.g., core logic, main services, orchestration, key data models, APIs, or entry points).\n\n"
    "A file is NOT critical if it is primarily:\n"
    "- tests, mocks, fixtures\n"
    "- documentation or examples\n"
    "- tutorials or demos\n"
    "- configuration-only or boilerplate with no domain logic\n"
    "- thin wrappers with no meaningful behavior\n\n"
    "If the file is NOT critical, output exactly: IGNORE\n\n"
    "If the file IS critical, write a concise and accurate summary that explains:\n"
    "- the files main purpose\n"
    "- its main components and what they do\n"
    "- how it interacts with other important files or components\n\n"
    "Do not quote code. Do not explain your reasoning. Do not add extra commentary."
)

PROMPT_USER_TEMPLATE = """
Repository:
{repo_name}

File:
{file_path}

Code:
{code}

Upstream dependencies (who calls or uses this file):
{upstream}

Downstream dependencies (what this file calls or uses):
{downstream}

Instructions:
- Decide whether this file is CRITICAL to the repository’s core functionality.
- If NOT critical, output exactly: IGNORE
- If CRITICAL, write a concise and accurate summary.

Preferred format for critical files:
"`{file_path}` <main purpose>. It defines <key components> that <primary responsibility>. "
"It works with <other files or modules> to <explain the interaction or flow>."

Output rules:
- Output ONLY the description or IGNORE
- Be concise, concrete, and readable
- No bullet points, no markdown, no extra text
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

    async def run(self, repo_name: str, local_repo_path: Path):
        self.lsp_cache = LSPCacheReader(str(local_repo_path))
        self.repo_name = repo_name
        self.local_repo_path = local_repo_path
        logger.info("Job %s: starting mental model generation for %s", self.job_id, repo_name)

        try:
            dir_tree = self._build_dir_tree()
            critical_files, ignored_files = await self.identify_critical_files(dir_tree, repo_name)
            logger.info(
                "Job %s: generated overview with %s critical files, ignored %s files",
                self.job_id,
                len(critical_files),
                len(ignored_files),
            )
            repo_context_token_count = await self.create_repo_context(repo_name)

            return len(critical_files), len(ignored_files), repo_context_token_count

        except Exception as e:
            logger.exception("Job %s: mental model generation error", self.job_id)
            traceback.print_exc()
            raise e

    async def identify_critical_files(self, dir_tree: List[str], repo_name: str) -> Tuple[List[Dict[str, str]], Set[str]]:
        """Generate a comprehensive overview of the repo by summarizing critical files, ignoring non-critical ones."""

        semaphore = asyncio.Semaphore(self.max_concurrency)
        retrieval = ContextualRetrieval(repo_name)

        async def summarize_file(file_path: str) -> tuple[str, str, str | None]:
            try:
                # file_path is relative to repo root, resolve it
                full_path = self.local_repo_path / file_path
                code = full_path.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Job %s: unable to read %s; marking ignored", self.job_id, file_path)
                return file_path, "IGNORE", None

            cached = self.mental_model_collection.find_one(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "document_type": {"$in": [MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]]},
                },
                {"_id": 0, "data": 1},
            )
            if cached:
                return file_path, cached["data"], code

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
            return file_path, response.strip(), code

        all_files = dir_tree

        insights: List[Dict[str, str]] = []
        ignored: Set[str] = set()

        pbar = tqdm(total=len(all_files), desc="Processing files")

        for i in range(0, len(all_files), self.batch_size):
            batch = all_files[i : i + self.batch_size]
            tasks = [summarize_file(fp) for fp in batch]
            results = await asyncio.gather(*tasks)

            files_to_index: list[tuple[str, str]] = []
            for fp, summary, code in results:
                if summary == "IGNORE":
                    ignored.add(fp)
                    self._upsert_document(
                        {
                            "repo_name": repo_name,
                            "file_path": fp,
                            "document_type": MENTAL_MODEL_TYPES["IGNORED"],
                            "data": "IGNORE",
                        }
                    )
                else:
                    insights.append({"file_path": fp, "summary": summary})
                    self._upsert_document(
                        {
                            "repo_name": repo_name,
                            "file_path": fp,
                            "document_type": MENTAL_MODEL_TYPES["BRIEF"],
                            "data": summary,
                        }
                    )
                    if code is not None:
                        files_to_index.append((fp, code))

            # Index files for contextual retrieval in parallel
            if files_to_index:
                for fp, code in files_to_index:
                    try:
                        logger.info(f"Indexing file for contextual retrieval: {fp}")
                        await retrieval.index_file(fp, code)
                    except Exception as e:
                        logger.warning("Job %s: failed to index %s: %s", self.job_id, fp, e)

            pbar.update(len(batch))

        pbar.close()

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
