from pathlib import Path
from typing import List, Tuple
from tree_sitter_languages import get_parser, get_language
from app.lsp_reference_resolver.core.base_analyser import BaseLSPAnalyzer

JAVA_QUERY = r"""
(method_invocation
  name: (identifier) @name
  arguments: (argument_list) @reference.call)

(type_list
  (type_identifier) @name) @reference.implementation

(object_creation_expression
  type: (type_identifier) @name) @reference.class

(superclass (type_identifier) @name) @reference.class
"""

class JavaAnalyzer(BaseLSPAnalyzer):
    def get_server_command(self) -> List[str]:
        # jdtls needs a workspace dir; pass repo path as -data
        return ["jdtls", "-data", str(self.repo_path)]

    def get_file_extensions(self) -> List[str]:
        return [".java"]

    def get_language_id(self) -> str:
        return "java"
    
    def needs_did_open(self) -> bool:
        return True

    def get_max_concurrency(self) -> int:
        import os
        return min(32, max(8, (os.cpu_count() or 8)))

    def get_warmup_seconds(self) -> float:
        return 120.0

    def ref_pos_extractor(self, text: str, path: Path) -> List[Tuple[int, int]]:
        lang = get_language("java")
        parser = get_parser("java")
        tree = parser.parse(text.encode("utf-8"))
        query = lang.query(JAVA_QUERY)

        out: List[Tuple[int,int]] = []
        seen = set()
        for node, cap_name in query.captures(tree.root_node):
            if cap_name != "name":
                continue
            pos = (node.start_point[0], node.start_point[1])
            if pos not in seen:
                seen.add(pos)
                out.append(pos)
        return out
