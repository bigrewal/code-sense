import json, logging, asyncio
from pathlib import Path

from ..db import get_mongo_client
from .core.base_analyser import BaseLSPAnalyzer
from .languages.python_analyser import PythonAnalyzer
from .languages.scala_analyser import ScalaAnalyzer
from .languages.java_analyser import JavaAnalyzer
from .languages.rust_analyser import RustAnalyzer
from typing import List
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class CodeAnalyzer:
    ANALYZERS = {"scala": ScalaAnalyzer, "python": PythonAnalyzer, "java": JavaAnalyzer, "rust": RustAnalyzer}

    def __init__(self, repo: Path, repo_id: str, job_id: str):
        self.repo = repo.resolve()
        self.base_repo_path = repo_id
        self.job_id = job_id

    def detect(self):
        langs = []
        for k, Cls in self.ANALYZERS.items():
            if Cls(self.repo, self.base_repo_path).get_files():
                langs.append(k)
        return langs

    async def analyze(self, langs=None):
        langs = langs or self.detect()
        changed_files: List[str] = []
        mongo = get_mongo_client()

        mongo.upsert_ingestion_job(
            job_id=self.job_id,
            repo_name=self.base_repo_path,
            status="running",
            current_stage="resolve_refs",
            stage_status={"resolve_refs": {"status": "running"}},
        )

        for lang in langs:
            mongo.upsert_ingestion_job(
                job_id=self.job_id,
                repo_name=self.base_repo_path,
                status="running",
                current_stage="resolve_refs",
                stage_status={"resolve_refs": {"status": "running ", "language": lang}},
            )
            analyzer: BaseLSPAnalyzer = self.ANALYZERS[lang](self.repo, self.base_repo_path)
            logging.info(f"=== Analyzing {lang} ===")
            await analyzer.analyze()
            changed_files.extend(analyzer.changed_files)
        
        mongo.upsert_ingestion_job(
            job_id=self.job_id,
            repo_name=self.base_repo_path,
            status="running",
            stage_status={"resolve_refs": {"status": "completed"}},
        )

        return changed_files