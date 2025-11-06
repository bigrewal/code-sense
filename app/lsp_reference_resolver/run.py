import json, logging, asyncio
from pathlib import Path
from .core.base_analyser import BaseLSPAnalyzer
from .languages.python_analyser import PythonAnalyzer
from .languages.scala_analyser import ScalaAnalyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class CodeAnalyzer:
    ANALYZERS = {"scala": ScalaAnalyzer, "python": PythonAnalyzer}

    def __init__(self, repo: Path, repo_id: str):
        self.repo = repo.resolve()
        self.base_repo_path = repo_id

    def detect(self):
        langs = []
        for k, Cls in self.ANALYZERS.items():
            if Cls(self.repo, self.base_repo_path).get_files():
                langs.append(k)
        return langs

    async def analyze(self, langs=None):
        langs = langs or self.detect()
        all_maps = []
        for lang in langs:
            analyzer: BaseLSPAnalyzer = self.ANALYZERS[lang](self.repo, self.base_repo_path)
            logging.info(f"=== Analyzing {lang} ===")
            maps = await analyzer.analyze()
            all_maps.extend(maps)
        
        

        with open("mappings.json", "w", encoding="utf-8") as f:
            json.dump(all_maps, f, indent=2)
        return all_maps
