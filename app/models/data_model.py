
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pathlib import Path

@dataclass
class CodeFile:
    """Information about a single code file."""
    file_path: Path
    relative_path: str
    language: str
    content: str
    size: int


@dataclass
class ASTNode:
    """Representation of an AST node."""
    node_id: str
    node_type: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    parent_id: Optional[str] = None
    children_ids: List[str] = None
    file_path: str = ""
    is_definition: bool = False
    is_reference: bool = False
    name: str = "NONE"

