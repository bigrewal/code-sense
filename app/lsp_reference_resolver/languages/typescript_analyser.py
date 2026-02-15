from pathlib import Path
from typing import List, Tuple
from tree_sitter_languages import get_parser, get_language
from app.lsp_reference_resolver.core.base_analyser import BaseLSPAnalyzer

TYPE_SCRIPT_QUERY = r"""
; Type annotations
(type_annotation
  (type_identifier) @name)

; Class instantiation: new Foo() / new Foo<T>()
(new_expression
  constructor: (identifier) @name)

; Function/method calls: foo() and obj.method()
(call_expression
  function: (identifier) @name)
(call_expression
  function: (member_expression property: (property_identifier) @name))

; Class inheritance: extends Foo / implements Foo
(class_heritage
  (extends_clause (identifier) @name))
(class_heritage
  (implements_clause (type_identifier) @name))

; Interface extends
(interface_declaration
  (extends_type_clause (type_identifier) @name))
"""


class TypeScriptAnalyzer(BaseLSPAnalyzer):
    def get_server_command(self) -> List[str]:
        return ["typescript-language-server", "--stdio"]

    def get_file_extensions(self) -> List[str]:
        return [".ts", ".tsx"]

    def get_language_id(self) -> str:
        return "typescript"

    def needs_did_open(self) -> bool:
        return True

    def get_max_concurrency(self) -> int:
        return 8

    def get_timeout_seconds(self) -> float:
        return 120.0

    def get_total_server_instances(self) -> int:
        return 1

    def is_excluded_definition_path(self, path: Path) -> bool:
        parts = set(path.parts)
        exclude = {
            "node_modules", "dist", "build", ".next", "coverage", "out",
        }
        return not parts.isdisjoint(exclude)

    def ref_pos_extractor(self, text: str, path: Path) -> List[Tuple[int, int]]:
        lang = get_language("typescript")
        parser = get_parser("typescript")
        tree = parser.parse(text.encode("utf-8"))
        query = lang.query(TYPE_SCRIPT_QUERY)

        out: List[Tuple[int, int]] = []
        seen = set()
        for node, cap_name in query.captures(tree.root_node):
            if cap_name != "name":
                continue
            pos = (node.start_point[0], node.start_point[1])
            if pos not in seen:
                seen.add(pos)
                out.append(pos)
        return out
