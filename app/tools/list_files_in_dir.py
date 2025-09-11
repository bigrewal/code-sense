from pathlib import Path
from typing import List
import os

def list_files_in_dir(directory: str, recursive: bool = True) -> List[str]:
    """List files in a directory, excluding hidden files and hidden directories."""
    result: List[str] = []
    directory = Path(directory)

    if recursive:
        for root, dirs, files in os.walk(directory):
            # prune hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if not f.startswith("."):
                    result.append(str(Path(root) / f))
    else:
        for f in directory.iterdir():
            if f.is_file() and not f.name.startswith("."):
                result.append(str(f))

    return result
