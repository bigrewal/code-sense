from pathlib import Path
from typing import List
import traceback
def list_files_in_dir(directory: str, recursive: bool = True) -> List[str]:
    """List files in a directory."""
    if recursive:
        return [str(p) for p in Path(directory).rglob("*") if p.is_file()]
    return [str(p) for p in Path(directory).glob("*") if p.is_file()]