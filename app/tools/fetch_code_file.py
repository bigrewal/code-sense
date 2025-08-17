from pathlib import Path
import traceback
def fetch_code_file(file_path: str) -> str:
        if not file_path:
            return {"error": "Missing file_path parameter"}

        repo_path = "data"
        full_path = Path(repo_path) / file_path

        try:
            if not full_path.exists():
                print(f"File not found: {full_path}")
                return {"error": f"File not found: {full_path}"}
            code = full_path.read_text(encoding="utf-8")
            return {"code": code}
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to read file: {str(e)}"}
    