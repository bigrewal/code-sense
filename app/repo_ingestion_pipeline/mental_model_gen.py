import asyncio
import hashlib
import logging
import os
from pathlib import Path

from tqdm import tqdm

from ..config import Config
from ..db import get_db_client
from ..llm_grok import GrokLLM

logger = logging.getLogger(__name__)


MENTAL_MODEL_TYPES = {
    "BRIEF": "BRIEF_FILE_OVERVIEW",
    "IGNORED": "IGNORED_FILE",
}

PROMPT_SYSTEM = (
   "You analyze one repository file at a time.\n\n"
    "Step 1: classify file criticality.\n"
    "A file is CRITICAL if it directly implements/coordinates core product behavior, API surface, core data model, orchestration, or essential integration flow.\n"
    "A file is NOT critical if it is primarily tests, fixtures, docs, examples, demos, generated code, config-only, or thin wrappers with no meaningful domain behavior.\n\n"
    "If NOT critical, output exactly: IGNORE\n\n"
    "If the file is critical, output exactly a concise summary using the required sentence template.\n"
    "Be factual and concise. Use evidence from the code and surrounding repository structure.\n"
    "Infer the most likely upstream/downstream relationships with the other source files from imports, exports, call sites, framework conventions, and surrounding code structure rather than assuming there are none.\n"
    "No bullets, no markdown, no extra commentary."
)

PROMPT_USER_TEMPLATE = """
    Repository: {repo_name}
    File: {file_path}

    Code:
    {code}

    Instructions:
    - If NOT critical, output exactly: IGNORE
    - If CRITICAL, output one concise paragraph in this exact format:

    "`{file_path}` <what this file does and why it exists>. It does this by <how it works end-to-end, explicitly naming every major component/function/class defined in this file and each component's role>. It interacts upstream with <key files/modules that call or depend on this file> and downstream with <key files/modules/services this file calls or depends on>."

    Rules:
    - Keep it very concise: 100-200 words total.
    - Mention concrete identifiers when available.
    - The second sentence must mention every major component in this file.
    - Do not claim there are no upstream/downstream interactions unless the code itself clearly supports that conclusion.
    - No bullets, no markdown, no extra text.
    """.strip()


class MentalModelStage:
    """Stage for generating and storing the hierarchical mental model."""

    def __init__(self, llm_grok: GrokLLM, config: dict | None = None):
        config = config or {}
        self.db_client = get_db_client()
        self.llm_client = llm_grok
        self.job_id = config.get("job_id", "unknown")
        self.batch_size = int(config.get("batch_size", 20))
        self.max_concurrency = int(config.get("max_concurrency", 10))

    async def run(
        self,
        repo_name: str,
        local_repo_path: Path,
        file_changes: dict | None = None,
    ):
        self.repo_name = repo_name
        self.local_repo_path = local_repo_path
        logger.info("Job %s: starting mental model generation for %s", self.job_id, repo_name)

        try:
            dir_tree = self._build_dir_tree()
            files_to_process = self._select_files_to_process(
                repo_name=repo_name,
                all_files=dir_tree,
                file_changes=file_changes,
            )
            deleted_files = sorted((file_changes or {}).get("deleted_files", []))
            self._delete_removed_artifacts(repo_name, deleted_files)

            critical_files, ignored_files = await self.identify_critical_files(
                files_to_process, repo_name
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

        except Exception:
            logger.exception("Job %s: mental model generation error", self.job_id)
            raise

    async def identify_critical_files(
        self,
        dir_tree: list[str],
        repo_name: str,
    ) -> tuple[list[dict[str, str]], set[str]]:

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def summarize_file(file_path: str) -> tuple[str, str, str | None]:
            try:
                full_path = self.local_repo_path / file_path
                code_bytes = full_path.read_bytes()
                code = code_bytes.decode("utf-8")
                sha1 = hashlib.sha1(code_bytes).hexdigest()
            except Exception:
                logger.warning("Job %s: unable to read %s; marking ignored", self.job_id, file_path)
                return file_path, "IGNORE", None

            cached = self.db_client.find_mental_model_document(
                repo_name=repo_name,
                file_path=file_path,
                document_types=[MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]],
                sha1=sha1,
            )
            if cached:
                return file_path, cached["data"], sha1

            user_prompt = PROMPT_USER_TEMPLATE.format(
                repo_name=repo_name,
                file_path=file_path,
                code=code,
            )

            async with semaphore:
                response = await self.llm_client.generate_async(
                    prompt=user_prompt,
                    system_prompt=PROMPT_SYSTEM,
                    temperature=0.0,
                    max_tokens=150,
                )
            return file_path, response.strip(), sha1

        all_files = dir_tree

        insights: list[dict[str, str]] = []
        ignored: set[str] = set()

        pbar = tqdm(total=len(all_files), desc="Processing files")

        for i in range(0, len(all_files), self.batch_size):
            batch = all_files[i : i + self.batch_size]
            tasks = [summarize_file(fp) for fp in batch]
            results = await asyncio.gather(*tasks)

            for fp, summary, sha1 in results:
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

        return insights, ignored

    async def create_repo_context(self, repo_name: str) -> int:
        critical_files = set(self.db_client.get_critical_file_paths(repo_name))
        context_parts: list[str] = []

        for file_path in critical_files:
            brief = self.db_client.get_brief_file_overview(repo_name, file_path)
            if brief:
                context_parts.append(brief)

        repo_context = "\n\n".join(context_parts)
        repo_context_token_count = self.llm_client.count_tokens(repo_context)

        self.db_client.upsert_repo_context(repo_name, repo_context)

        return repo_context_token_count

    def _get_repo_file_classification_counts(self, repo_name: str) -> tuple[int, int]:
        critical_total = self.db_client.count_mental_model_documents(
            repo_name=repo_name,
            document_type=MENTAL_MODEL_TYPES["BRIEF"],
        )
        ignored_total = self.db_client.count_mental_model_documents(
            repo_name=repo_name,
            document_type=MENTAL_MODEL_TYPES["IGNORED"],
        )
        return critical_total, ignored_total

    def _build_dir_tree(self) -> list[str]:
        code_files = []
        supported_extensions = set(Config.SUPPORTED_LANGUAGES.keys())
        ignore_folders = Config.IGNORE_FOLDERS

        for root, dirs, files in os.walk(self.local_repo_path):
            root_path = Path(root)

            # Filter out ignored directories in-place
            dirs[:] = [d for d in dirs if d not in ignore_folders and not d.startswith(".")]

            for file in files:
                file_path = root_path / file
                if file_path.suffix in supported_extensions:
                    # Persist paths relative to repo root so they match stored mental-model docs.
                    relative_path = file_path.relative_to(self.local_repo_path)
                    code_files.append(str(relative_path))

        return sorted(code_files)

    def _upsert_document(self, document: dict):
        self.db_client.upsert_mental_model_document(
            repo_name=document["repo_name"],
            file_path=document["file_path"],
            document_type=document["document_type"],
            data=document["data"],
            sha1=document.get("sha1"),
        )

    def _replace_file_document(self, repo_name: str, file_path: str, doc_type: str, data: str, sha1: str):
        self.db_client.delete_mental_model_documents(
            repo_name=repo_name,
            file_paths=[file_path],
            document_types=[MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]],
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

    def _delete_removed_artifacts(
        self,
        repo_name: str,
        deleted_files: list[str],
    ):
        if not deleted_files:
            return
        self.db_client.delete_mental_model_documents(
            repo_name=repo_name,
            file_paths=deleted_files,
            document_types=[MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]],
        )

    def _select_files_to_process(
        self,
        repo_name: str,
        all_files: list[str],
        file_changes: dict | None,
    ) -> list[str]:
        if not file_changes:
            return all_files

        selected: set[str] = set()
        selected.update(file_changes.get("new_files", []))
        selected.update(file_changes.get("changed_files", []))

        # Backfill missing mental-model docs even when there are no file deltas.
        existing = self.db_client.list_mental_model_documents(
            repo_name=repo_name,
            document_types=[MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]],
        )
        documented_files = {doc.get("file_path") for doc in existing if doc.get("file_path")}
        for path in all_files:
            if path not in documented_files:
                selected.add(path)

        return sorted(set(all_files) & selected)
