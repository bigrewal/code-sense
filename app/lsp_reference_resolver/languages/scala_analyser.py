from pathlib import Path
from typing import List, Tuple
from tree_sitter_languages import get_parser, get_language
from ..core.base_analyser import BaseLSPAnalyzer

SCALA_QUERY = r"""
; calls: foo(...) and obj.foo(...)
(call_expression function: (identifier) @id)
(call_expression function: (field_expression field: (identifier) @id))

; instantiation: new Foo / new Foo[A]
(instance_expression (type_identifier) @id)
(instance_expression (generic_type (type_identifier) @id))

; inheritance: extends Foo / extends Foo[A]
(extends_clause (type_identifier) @id)
(extends_clause (generic_type (type_identifier) @id))

"""

class ScalaAnalyzer(BaseLSPAnalyzer):
    def get_server_command(self): return ["metals"]
    def get_file_extensions(self): return [".scala", ".sbt", ".sc"]
    def get_language_id(self): return "scala"

    def needs_did_open(self) -> bool:
        return False  # Metals indexes workspace fine

    def get_warmup_seconds(self) -> float:
        return 120.0

    def get_max_concurrency(self) -> int:
        import os
        return min(32, max(8, (os.cpu_count() or 8)))
    
    def is_excluded_definition_path(self, path: Path) -> bool:
        parts = set(path.parts)
        # Ignore Metals / build junk inside repo
        exclude = {
            ".metals", ".bloop", "project", "target",
        }
        return not parts.isdisjoint(exclude)

    def ref_pos_extractor(self, text: str, path: Path) -> List[Tuple[int, int]]:
        lang = get_language("scala")
        parser = get_parser("scala")
        tree = parser.parse(text.encode())
        query = lang.query(SCALA_QUERY)

        out: List[Tuple[int, int]] = []
        for node, _ in query.captures(tree.root_node):
            # Capture start row/col (0-based)
            out.append((node.start_point[0], node.start_point[1]))

        # Deduplicate while preserving order
        seen = set()
        uniq = []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq
