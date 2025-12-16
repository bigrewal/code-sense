from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from ..llm_grok import GrokLLM
from ..config import Config
from ..db import get_mongo_client


@dataclass
class FileMetric:
    file_path: Path
    tokens: int
    language: str

class PreIngestionAnalysisError(ValueError):
    """Raised when the repository fails pre-ingestion validation."""

    def __init__(self, message: str, *, metrics: Optional[Dict[str, Any]] = None, code: str = "PRECHECK_FAILED"):
        super().__init__(message)
        self.code = code
        self.metrics = metrics or {}


class PreIngestionAnalysisStage:
    """Stage to perform pre-ingestion analysis on a repository."""

    def __init__(self, llm_grok: GrokLLM, job_id: str, repo_name: str):
        self.llm_grok = llm_grok
        self.job_id = job_id
        self.repo_name = repo_name

    def run(self, repo_path: Path) -> Dict[str, Any]:
        mongo = get_mongo_client()
        mongo.upsert_ingestion_job(
            job_id=self.job_id,
            repo_name=self.repo_name,
            status="running",
            current_stage="precheck",
            stage_status={"precheck": {"status": "running"}},
        )

        file_metrics = self.scan(repo_path)
        summary = self.summarize(file_metrics)
        self.validate(summary)

        mongo.upsert_ingestion_job(
            job_id=self.job_id,
            repo_name=self.repo_name,
            status="running",
            stage_status={
                "precheck": {
                    "status": "completed",
                    "metrics": summary,
                }
            },
        )

        return summary
    
    def _is_excluded(self, path: Path, repo_path: Path) -> bool:
        try:
            relative_parts = path.relative_to(repo_path).parts
        except ValueError:
            return False

        return any(marker in relative_parts for marker in Config.IGNORE_FOLDERS)


    def _classify_language(self, path: Path) -> Optional[str]:
        return Config.SUPPORTED_LANGUAGES.get(path.suffix.lower())

    def scan(self, repo_path: Path) -> List[FileMetric]:
        """
        Enumerate known-language files and compute per-file metrics.
        Returns metrics only for files whose extensions appear in self.lang_map.
        """
        repo_path = Path(repo_path)
        metrics: List[FileMetric] = []

        for file_path in repo_path.rglob("*"):
            if not file_path.is_file():
                continue
            if self._is_excluded(file_path, repo_path=repo_path):
                continue

            language = self._classify_language(file_path)
            if not language:
                continue  # unknown language/extension (ignored for token totals)

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                tok = self.llm_grok.count_tokens(content)
                rel = str(file_path.relative_to(repo_path))
                metrics.append(FileMetric(file_path=rel, language=language, tokens=tok))
            except Exception:
                continue

        return metrics
    
    def summarize(self, file_metrics: List[FileMetric]) -> Dict[str, Any]:
        tokens_by_lang: Dict[str, int] = {}
        files_by_lang: Dict[str, int] = {}
        total_tokens = 0

        for m in file_metrics:
            tokens_by_lang[m.language] = tokens_by_lang.get(m.language, 0) + m.tokens
            files_by_lang[m.language] = files_by_lang.get(m.language, 0) + 1
            total_tokens += m.tokens

        supported_tokens = sum(tokens_by_lang.get(lang, 0) for lang in Config.SUPPORTED_LANGUAGES.values())
        supported_ratio = (supported_tokens / total_tokens) if total_tokens > 0 else 0.0

        return {
            "total_tokens": total_tokens,
            "supported_tokens": supported_tokens,
            "supported_ratio": supported_ratio,
            "tokens_by_lang": tokens_by_lang,
            "files_by_lang": files_by_lang,
            "supported_languages": sorted(Config.SUPPORTED_LANGUAGES.values()),
            "min_supported_ratio": Config.min_supported_ratio,
            "known_extensions": sorted(Config.SUPPORTED_LANGUAGES.keys()),
            "file_count": len(file_metrics),
        }

    def validate(self, summary: Dict[str, Any]) -> None:
        total_tokens = int(summary.get("total_tokens") or 0)
        supported_ratio = float(summary.get("supported_ratio") or 0.0)

        if total_tokens <= 0:
            raise PreIngestionAnalysisError(
                "Pre-ingestion check failed: no tokens counted in known-language files.",
                metrics=summary,
                code="NO_KNOWN_SOURCE_FILES",
            )

        if supported_ratio < Config.min_supported_ratio:
            pct = round(supported_ratio * 100, 2)
            min_pct = round(Config.min_supported_ratio * 100, 2)
            raise PreIngestionAnalysisError(
                f"Pre-ingestion check failed: supported languages account for {pct}% of tokens (min {min_pct}%).",
                metrics=summary,
                code="UNSUPPORTED_LANGUAGE_MIX",
            )
