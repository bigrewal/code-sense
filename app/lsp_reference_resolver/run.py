import logging
from pathlib import Path

from .core.base_analyser import BaseLSPAnalyzer
from .languages.python_analyser import PythonAnalyzer
from .languages.scala_analyser import ScalaAnalyzer
from .languages.java_analyser import JavaAnalyzer
from .languages.rust_analyser import RustAnalyzer
from .languages.typescript_analyser import TypeScriptAnalyzer

from typing import List, Set, Dict, Any
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class CodeAnalyzer:
    ANALYZERS = {"scala": ScalaAnalyzer, "python": PythonAnalyzer, "java": JavaAnalyzer, "rust": RustAnalyzer, "typescript": TypeScriptAnalyzer}

    def __init__(self, repo: Path, repo_name: str, job_id: str):
        self.repo = repo.resolve()
        self.base_repo_path = repo_name
        self.job_id = job_id

    def detect(self):
        langs = []
        for k, Cls in self.ANALYZERS.items():
            if Cls(self.repo, self.base_repo_path).get_files():
                langs.append(k)
        return langs

    async def analyze(self, langs=None):
        langs = langs or self.detect()
        changed_files: Set[str] = set()
        content_changed_files: Set[str] = set()
        impacted_ref_files: Set[str] = set()
        deleted_files: Set[str] = set()

        for lang in langs:
            analyzer: BaseLSPAnalyzer = self.ANALYZERS[lang](self.repo, self.base_repo_path)
            logging.info(f"=== Analyzing {lang} ===")
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
