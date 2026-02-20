import logging
from pathlib import Path
from typing import Optional, Sequence

from .core.base_analyser import BaseLSPAnalyzer
from .languages.java_analyser import JavaAnalyzer
from .languages.python_analyser import PythonAnalyzer
from .languages.rust_analyser import RustAnalyzer
from .languages.scala_analyser import ScalaAnalyzer
from .languages.typescript_analyser import TypeScriptAnalyzer

logger = logging.getLogger(__name__)


class CodeAnalyzer:
    ANALYZERS = {
        "scala": ScalaAnalyzer,
        "python": PythonAnalyzer,
        "java": JavaAnalyzer,
        "rust": RustAnalyzer,
        "typescript": TypeScriptAnalyzer,
    }

    def __init__(self, repo: Path, repo_name: str, job_id: str):
        self.repo = repo.resolve()
        self.base_repo_path = repo_name

    def detect(self) -> list[str]:
        return [
            lang
            for lang, analyzer_cls in self.ANALYZERS.items()
            if analyzer_cls(self.repo, self.base_repo_path).get_files()
        ]

    async def analyze(self, langs: Optional[Sequence[str]] = None) -> dict[str, list[str]]:
        langs = list(langs or self.detect())
        changed_files: set[str] = set()
        content_changed_files: set[str] = set()
        impacted_ref_files: set[str] = set()
        deleted_files: set[str] = set()

        for lang in langs:
            analyzer: BaseLSPAnalyzer = self.ANALYZERS[lang](self.repo, self.base_repo_path)
            logger.info("=== Analyzing %s ===", lang)
            await analyzer.analyze()
            changed_files.update(analyzer.changed_files)
            content_changed_files.update(analyzer.content_changed_files)
            impacted_ref_files.update(analyzer.impacted_ref_files)
            deleted_files.update(analyzer.deleted_files)

        return {
            "changed_files": sorted(changed_files),
            "content_changed_files": sorted(content_changed_files),
            "impacted_ref_files": sorted(impacted_ref_files),
            "deleted_files": sorted(deleted_files),
        }
