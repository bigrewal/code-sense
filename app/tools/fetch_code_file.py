from pathlib import Path
import traceback
def fetch_code_file(file_path: str) -> str:
        if not file_path:
            return {"error": "Missing file_path parameter"}

        if not file_path.startswith("data/"):
            return f"File path: {file_path} is not valid."

        full_path = Path(file_path)

        try:
            if not full_path.exists():
                print(f"File not found: {full_path}")
                return {"error": f"File not found: {full_path}"}
            code = full_path.read_text(encoding="utf-8")
            # print(f"Fetched code from {full_path}")
            return code
        except Exception as e:
            traceback.print_exc()
            return {"error": f"Failed to read file: {str(e)}"}
    