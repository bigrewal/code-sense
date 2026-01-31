from pathlib import Path
import traceback
import json
from ..config import Config

def fetch_code_file(repo_name: str, file_path: str) -> str:
    if not file_path:
        return json.dumps({"error": "Missing file_path parameter"})

    full_path = Path(Config.BASE_REPO_DIR) / repo_name / file_path
    try:
        if not full_path.exists():
            return json.dumps({"error": f"File not found: {str(full_path)}"})
        return full_path.read_text(encoding="utf-8")
    except Exception as e:
        traceback.print_exc()
        return json.dumps({"error": f"Failed to read file: {str(e)}"})


fetch_code_file_tool = {
    "type": "function",
    "function": {
        "name": "fetch_code_file",
        "description": "Fetches the code of a repository code file by path.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (must start with 'data/').",
                }
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
    },
}
