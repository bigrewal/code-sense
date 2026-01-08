from pathlib import Path
from typing import List, Tuple
from tree_sitter_languages import get_parser, get_language
from app.lsp_reference_resolver.core.base_analyser import BaseLSPAnalyzer

PY_QUERY = r"""
(call
  function: [
    (identifier) @name
    (attribute attribute: (identifier) @name)
  ]
) @reference.call
"""

class PythonAnalyzer(BaseLSPAnalyzer):
    def get_server_command(self): return ["pylsp"]
    def get_file_extensions(self): return [".py"]
    def get_language_id(self): return "python"

    def needs_did_open(self) -> bool:
        return False

    def get_max_concurrency(self) -> int:
        return 8

    def get_warmup_seconds(self) -> float:
        return 0.0

    def get_timeout_seconds(self) -> float:
        return 120.0
    
    def get_total_server_instances(self) -> int:
        return 4

    def get_initialize_options(self) -> dict:
        return {"python": {"analysis": {"indexing": True}}}
    
    def is_excluded_definition_path(self, path: Path) -> bool:
        parts = set(path.parts)
        exclude = {
            "venv", ".venv", "__pycache__", "site-packages",
            ".mypy_cache", ".pytest_cache", ".ruff_cache",
        }
        return not parts.isdisjoint(exclude)

    def ref_pos_extractor(self, text: str, path: Path) -> List[Tuple[int, int]]:
        lang = get_language("python")
        parser = get_parser("python")
        tree = parser.parse(text.encode())
        query = lang.query(PY_QUERY)
        captures = query.captures(tree.root_node)

        out = []
        for node, cap_name in captures:
            p = node.parent
            if p is None:
                continue

            # Fast path: exclude obvious declaration-ish parents
            if p.type in {"parameters", "typed_parameter", "default_parameter"}:
                continue

            # Also skip keyword_identifier nodes (rare edge in some grammars)
            if p.type == "keyword_identifier":
                continue

            # At this point we consider it a "read"
            out.append((node.start_point[0], node.start_point[1]))
        return out