import os
from typing import Dict
from pathlib import Path

class MentalModelFetcher:
    def __init__(self, model_path: str = "data/dictquery/mental_model/mental_model.md"):
        self.model_path = model_path

    def fetch(self) -> Dict[str, any]:
        """Load and parse the mental model Markdown file into a structured dict."""
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"Mental model file not found: {self.model_path}")

        with open(self.model_path, "r") as f:
            content = f.read()

        return {"overview": content}
