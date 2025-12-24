import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter

from ..llm_grok import GrokLLM
from ..config import Config

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
    """Stage to perform pre-ingestion analysis on a repository.

    Note: This stage should NOT upsert job status itself.
    The pipeline should own status transitions (RUNNING/DONE/FAILED).
    """

    def __init__(self, llm_grok: GrokLLM, job_id: str, repo_name: str):
        self.llm_grok = llm_grok
        self.job_id = job_id
        self.repo_name = repo_name

    def run(self, repo_path: Path) -> Dict[str, Any]:
        file_metrics, scan_stats = self.scan(repo_path)
        summary = self.summarize(file_metrics=file_metrics, scan_stats=scan_stats)
        self.validate(summary)
        return summary

    def _is_excluded(self, path: Path, repo_path: Path) -> bool:
        if path.suffix in {".sqlite", ".sqlite-shm", ".sqlite-wal"}:
            return True
        try:
            relative_parts = path.relative_to(repo_path).parts
        except ValueError:
            return False
        return any(marker in relative_parts for marker in Config.IGNORE_FOLDERS)

    def _classify_language(self, path: Path) -> Optional[str]:
        # Map suffix -> language name (supported languages only)
        return Config.SUPPORTED_LANGUAGES.get(path.suffix.lower())

    def scan(self, repo_path: Path) -> Tuple[List[FileMetric], Dict[str, Any]]:
        """
        Enumerate files and compute per-file token metrics using the LLM tokenizer.

        We record BOTH supported and unsupported files:
        - supported: file extension in Config.SUPPORTED_LANGUAGES
        - unsupported: everything else (still token-counted for meaningful ratios)

        We also track exclusion stats for transparency.
        """
        repo_path = Path(repo_path)

        metrics: List[FileMetric] = []
        excluded_file_count = 0
        excluded_paths_counter: Counter[str] = Counter()

        total_files_seen = 0
        total_files_tokenized = 0
        tokenization_errors = 0

        for file_path in repo_path.rglob("*"):
            if not file_path.is_file():
                continue

            total_files_seen += 1

            if self._is_excluded(file_path, repo_path=repo_path):
                excluded_file_count += 1
                try:
                    rel_parts = file_path.relative_to(repo_path).parts
                    # record the top-level folder when possible for useful reporting
                    if len(rel_parts) > 0:
                        excluded_paths_counter[rel_parts[0]] += 1
                except Exception:
                    pass
                continue

            language = self._classify_language(file_path)
            supported = bool(language)

            # Give unsupported a stable label so users can see the mix.
            language_label = language if supported else "unsupported/unknown"

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                tok = self.llm_grok.count_tokens(content)
                rel = str(file_path.relative_to(repo_path))
                metrics.append(
                    FileMetric(
                        file_path=rel,
                        language=language_label,
                        tokens=tok,
                        supported=supported,
                    )
                )
                total_files_tokenized += 1
            except Exception as e:
                logger.warning(f"Tokenization error for file {file_path}: {e}")
                tokenization_errors += 1
                continue

        scan_stats = {
            "total_files_seen": total_files_seen,
            "total_files_tokenized": total_files_tokenized,
            "tokenization_errors": tokenization_errors,
            "excluded_file_count": excluded_file_count,
            "excluded_paths_top": [
                {"path": p, "count": c}
                for p, c in excluded_paths_counter.most_common(10)
            ],
            "ignore_folders": list(Config.IGNORE_FOLDERS),
        }
        return metrics, scan_stats

    def summarize(self, file_metrics: List[FileMetric], scan_stats: Dict[str, Any]) -> Dict[str, Any]:
        tokens_by_lang: Dict[str, int] = {}
        files_by_lang: Dict[str, int] = {}

        total_tokens = 0
        supported_tokens = 0
        unsupported_tokens = 0
        supported_file_count = 0
        unsupported_file_count = 0

        # Outliers / helpful user-facing items
        largest_files = sorted(file_metrics, key=lambda m: m.tokens, reverse=True)[:10]
        max_file_tokens = largest_files[0].tokens if largest_files else 0
        avg_tokens_per_file = int(total_tokens / len(file_metrics)) if file_metrics else 0  # will be overwritten after loop

        for m in file_metrics:
            tokens_by_lang[m.language] = tokens_by_lang.get(m.language, 0) + m.tokens
            files_by_lang[m.language] = files_by_lang.get(m.language, 0) + 1

            total_tokens += m.tokens
            if m.supported:
                supported_tokens += m.tokens
                supported_file_count += 1
            else:
                unsupported_tokens += m.tokens
                unsupported_file_count += 1

        avg_tokens_per_file = int(total_tokens / len(file_metrics)) if file_metrics else 0
        supported_ratio = (supported_tokens / total_tokens) if total_tokens > 0 else 0.0

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

        return {
            # Verdict-ish fields (validate() will still raise on failure)
            "passes_precheck": True,

            # High-level counts
            "total_files_seen": scan_stats.get("total_files_seen", 0),
            "total_files_tokenized": scan_stats.get("total_files_tokenized", 0),
            "tokenization_errors": scan_stats.get("tokenization_errors", 0),

            "excluded_file_count": scan_stats.get("excluded_file_count", 0),
            "excluded_paths_top": scan_stats.get("excluded_paths_top", []),

            "supported_file_count": supported_file_count,
            "unsupported_file_count": unsupported_file_count,
            "tokenized_file_count": len(file_metrics),

            # Token totals
            "total_tokens": total_tokens,
            "supported_tokens": supported_tokens,
            "unsupported_tokens": unsupported_tokens,
            "supported_ratio": supported_ratio,
            "min_supported_ratio": Config.min_supported_ratio,

            # Language insight
            "supported_languages": supported_langs,
            "primary_language": primary_language,
            "language_distribution_pct": language_distribution_pct,

            # Raw breakdowns (still useful for debugging / power users)
            "tokens_by_lang": tokens_by_lang,
            "files_by_lang": files_by_lang,

            # Outliers / cost drivers
            "max_file_tokens": max_file_tokens,
            "avg_tokens_per_file": avg_tokens_per_file,
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

            # Placeholders for UX; pipeline/UI can display these directly
            "recommendations": [],
        }

    def validate(self, summary: Dict[str, Any]) -> None:
        total_tokens = int(summary.get("total_tokens") or 0)
        supported_ratio = float(summary.get("supported_ratio") or 0.0)

        if total_tokens <= 0:
            summary["passes_precheck"] = False
            summary.setdefault("recommendations", []).append(
                "No tokens counted. Ensure the repository contains readable source files and is not entirely excluded."
            )
            raise PreIngestionAnalysisError(
                "Pre-ingestion check failed: no tokens counted.",
                metrics=summary,
                code="NO_TOKENS_COUNTED",
            )

        if supported_ratio < Config.min_supported_ratio:
            summary["passes_precheck"] = False
            pct = round(supported_ratio * 100, 2)
            min_pct = round(Config.min_supported_ratio * 100, 2)

            # Add actionable recommendations
            recs = summary.setdefault("recommendations", [])
            excluded_paths = summary.get("excluded_paths_top") or []
            if excluded_paths:
                recs.append("Review excluded folders; large exclusions can skew token counts.")
            recs.append("Consider adding additional language support or excluding non-source/generated content.")
            largest = summary.get("largest_files") or []
            if largest:
                recs.append("Check largest files; generated/minified artifacts can dominate token usage.")

            raise PreIngestionAnalysisError(
                f"Pre-ingestion check failed: supported languages account for {pct}% of tokens (min {min_pct}%).",
                metrics=summary,
                code="UNSUPPORTED_LANGUAGE_MIX",
            )
