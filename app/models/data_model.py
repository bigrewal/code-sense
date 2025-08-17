
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pathlib import Path

@dataclass
class UploadedRepository:
    """Data structure for uploaded repository information."""
    repo_id: str
    original_filename: str
    file_size: int
    upload_timestamp: str
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = None


@dataclass
class S3StorageInfo:
    """Information about repository stored in S3."""
    bucket_name: str
    s3_key: str
    local_path: Path
    repo_info: UploadedRepository

@dataclass
class ReferenceResolutionResult:
    """Result from tree-sitter reference resolution."""
    repo_path: Path
    references: Dict[str, List[tuple]]  # Mapping from file path to list of references
    total_references: int
    resolved_count: int
    unresolved_count: int
    processing_time: float

@dataclass
class CodeFile:
    """Information about a single code file."""
    file_path: Path
    relative_path: str
    language: str
    content: str
    size: int


@dataclass
class ReferenceResolutionResult:
    """Result from tree-sitter reference resolution."""
    repo_path: Path
    references: Dict[str, List[tuple]]  # Mapping from file path to list of references
    total_references: int
    resolved_count: int
    unresolved_count: int
    processing_time: float


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


@dataclass
class CodeGraph:
    """Graph representation of the codebase."""
    repo_id: str
    nodes: List[ASTNode]
    edges: List[Dict[str, Any]]  # Graph edges with metadata
    # files_processed: List[CodeFile]
    total_nodes: int
    total_edges: int
