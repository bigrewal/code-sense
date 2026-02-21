import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tqdm import tqdm
from ..config import Config
from ..db import get_mongo_client
from ..llm_grok import GrokLLM
from ..models.data_model import JobAborted
from .file_state import RepoFileChanges, build_repo_file_changes

logger = logging.getLogger(__name__)


@dataclass
class FileMetric:
    file_path: str
    tokens: int
    language: str
    supported: bool


class PreIngestionAnalysisError(ValueError):
    """Raised when the repository fails pre-ingestion validation."""

    def __init__(
        self,
        message: str,
        *,
        metrics: Optional[Dict[str, Any]] = None,
        code: str = "PRECHECK_FAILED",
    ):
        super().__init__(message)
        self.code = code
        self.metrics = metrics or {}


class PreIngestionAnalysisStage:
    def __init__(self, llm_grok: GrokLLM, repo_name: str, should_abort=None):
        self.llm_grok = llm_grok
        self.repo_name = repo_name
        self.should_abort = should_abort

    async def run(
        self,
        repo_path: Path,
        file_changes: Optional[RepoFileChanges] = None,
        previous_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.should_abort and self.should_abort():
            raise JobAborted()

        mongo = get_mongo_client()
        previous_state = previous_state if previous_state is not None else mongo.get_repo_file_states(self.repo_name)
        file_changes = file_changes or build_repo_file_changes(repo_path=repo_path, previous_state=previous_state)

        file_metrics, scan_stats, state_rows = await self.scan(
            repo_path=repo_path,
            file_changes=file_changes,
            previous_state=previous_state,
        )
        mongo.upsert_repo_file_states(self.repo_name, state_rows)
        mongo.delete_repo_file_states(self.repo_name, list(file_changes.deleted_files))

        summary = self.summarize(file_metrics=file_metrics, scan_stats=scan_stats)
        summary.update(
            {
                "new_files": len(file_changes.new_files),
                "changed_files": len(file_changes.changed_files),
                "deleted_files": len(file_changes.deleted_files),
                "unchanged_files": len(file_changes.unchanged_files),
            }
        )
        self.validate(summary)
        return summary

    async def scan(
        self,
        repo_path: Path,
        file_changes: RepoFileChanges,
        previous_state: Dict[str, Any],
    ) -> Tuple[List[FileMetric], Dict[str, Any], List[Dict[str, Any]]]:
        metrics: List[FileMetric] = []
        state_rows: List[Dict[str, Any]] = []
        total_files_seen = len(file_changes.current_files)
        total_files_tokenized = 0
        tokenization_errors = 0

        sem = asyncio.Semaphore(10)
        lock = asyncio.Lock()

        changed_or_new = set(file_changes.changed_files) | set(file_changes.new_files)
        pbar = tqdm(total=len(file_changes.current_files), desc="Pre-analysis file scan", unit="file")

        async def process_one(rel: str) -> None:
            nonlocal tokenization_errors, total_files_tokenized

            if self.should_abort and self.should_abort():
                raise JobAborted()

            entry = file_changes.current_files[rel]
            supported = entry.supported
            language_label = entry.language if supported else "unsupported/unknown"

            try:
                if supported:
                    if rel in changed_or_new or (previous_state.get(rel) or {}).get("token_count") is None:
                        content = await asyncio.to_thread((repo_path / rel).read_text, encoding="utf-8", errors="ignore")
                        async with sem:
                            tok = await asyncio.to_thread(self.llm_grok.count_tokens, content)
                        async with lock:
                            total_files_tokenized += 1
                    else:
                        tok = int((previous_state.get(rel) or {}).get("token_count", 0))
                else:
                    tok = 0

                metric = FileMetric(
                    file_path=rel,
                    language=language_label,
                    tokens=tok,
                    supported=supported,
                )

                async with lock:
                    metrics.append(metric)
                    state_rows.append(
                        {
                            "file_path": rel,
                            "sha1": entry.sha1,
                            "language": entry.language,
                            "supported": supported,
                            "token_count": tok,
                        }
                    )

            except Exception as e:
                logger.warning("Tokenization error for file %s: %s", rel, e)
                async with lock:
                    tokenization_errors += 1
            finally:
                pbar.update(1)

        try:
            tasks = [asyncio.create_task(process_one(rel)) for rel in sorted(file_changes.current_files.keys())]
            await asyncio.gather(*tasks)
        finally:
            pbar.close()

        scan_stats = {
            "total_files_seen": total_files_seen,
            "total_files_tokenized": total_files_tokenized,
            "tokenization_errors": tokenization_errors,
            "excluded_file_count": 0,
            "excluded_paths_top": [],
            "ignore_folders": list(Config.IGNORE_FOLDERS),
        }
        return metrics, scan_stats, state_rows

    def summarize(self, file_metrics: List[FileMetric], scan_stats: Dict[str, Any]) -> Dict[str, Any]:
        tokens_by_lang: Dict[str, int] = {}
        files_by_lang: Dict[str, int] = {}

        supported_tokens = 0
        supported_file_count = 0
        unsupported_file_count = 0

        # Outliers / helpful user-facing items
        largest_files = sorted(file_metrics, key=lambda m: m.tokens, reverse=True)[:10]
        max_file_tokens = largest_files[0].tokens if largest_files else 0

        for m in file_metrics:
            tokens_by_lang[m.language] = tokens_by_lang.get(m.language, 0) + m.tokens
            files_by_lang[m.language] = files_by_lang.get(m.language, 0) + 1

            if m.supported:
                supported_tokens += m.tokens
                supported_file_count += 1
            else:
                # unsupported_tokens += m.tokens
                unsupported_file_count += 1

        # Compute language distribution only for supported languages (more meaningful),
        # but keep unsupported/unknown visible separately.
        supported_langs = sorted(set(Config.SUPPORTED_LANGUAGES.values()))
        supported_tokens_by_lang = {k: v for k, v in tokens_by_lang.items() if k in supported_langs}

        # Primary language by supported tokens (fallback to any language)
        primary_language = None
        if supported_tokens_by_lang:
            primary_language = max(supported_tokens_by_lang.items(), key=lambda kv: kv[1])[0]
        elif tokens_by_lang:
            primary_language = max(tokens_by_lang.items(), key=lambda kv: kv[1])[0]

        # Percentage distribution (supported only)
        total_supported_for_pct = sum(supported_tokens_by_lang.values())
        language_distribution_pct = (
            {k: round((v / total_supported_for_pct) * 100, 2) for k, v in supported_tokens_by_lang.items()}
            if total_supported_for_pct > 0
            else {}
        )

        single_language_dominant = False
        if language_distribution_pct:
            top_pct = max(language_distribution_pct.values())
            single_language_dominant = top_pct >= 70.0
        
        total_files_seen = scan_stats.get("total_files_seen", 0)
        total_files_tokenized = scan_stats.get("total_files_tokenized", 0)


        return {
            "passes_precheck": True,

            # High-level counts
            "total_files_seen": total_files_seen,
            "total_files_tokenized": total_files_tokenized,
            "tokenization_errors": scan_stats.get("tokenization_errors", 0),

            "excluded_file_count": scan_stats.get("excluded_file_count", 0),
            "excluded_paths_top": scan_stats.get("excluded_paths_top", []),

            "supported_file_count": supported_file_count,
            "unsupported_file_count": unsupported_file_count,
            "coverage_ratio": (total_files_tokenized / total_files_seen) if total_files_seen else 0,

            # Token totals
            "supported_tokens": supported_tokens,
            "min_supported_cov_ratio": Config.min_supported_cov_ratio,

            # Language insight
            "supported_languages": supported_langs,
            "primary_language": primary_language,
            "language_distribution_pct": language_distribution_pct,

            # Raw breakdowns (still useful for debugging / power users)
            "tokens_by_lang": tokens_by_lang,
            "files_by_lang": files_by_lang,

            # Outliers / cost drivers
            "max_file_tokens": max_file_tokens,
            "largest_files": [
                {
                    "path": m.file_path,
                    "tokens": m.tokens,
                    "language": m.language,
                    "supported": m.supported,
                }
                for m in largest_files
            ],

            # Quick heuristics
            "single_language_dominant": single_language_dominant,

            "recommendations": [],
        }

    def validate(self, summary: Dict[str, Any]) -> None:
        supported_tokens = int(summary.get("supported_tokens") or 0)

        if supported_tokens <= 0:
            summary["passes_precheck"] = False
            summary.setdefault("recommendations", []).append(
                "No tokens counted. Ensure the repository contains supported files."
            )
            raise PreIngestionAnalysisError(
                "Pre-ingestion check failed: no tokens counted.",
                metrics=summary,
                code="NO_TOKENS_COUNTED",
            )
