from pathlib import Path
from typing import List, Tuple
from tree_sitter_languages import get_parser, get_language
from app.lsp_reference_resolver.core.base_analyser import BaseLSPAnalyzer

PY_QUERY = r"""
[
  (identifier) @name
  (attribute
    attribute: (identifier) @name)
]
(#not-any-of? @name "self" "cls")
"""

# Parent/field pairs where an identifier is a *binding*, not a read
BINDING_SLOTS = {
    ("function_definition", "name"),
    ("class_definition", "name"),
    ("keyword_argument", "name"),         # foo(arg=...) — 'arg' is a label, not a read
    ("aliased_import", "alias"),          # import x as y   -> 'y' is a new name
    ("with_item", "alias"),               # with ctx as y
    ("except_clause", "name"),            # except E as e
    ("for_statement", "left"),            # for x in ...
    ("assignment", "left"),               # x = ...
    ("augmented_assignment", "left"),     # x += ...
    ("pattern_list", None),               # match/case bindings can show up here
    ("as_pattern", None),                 # case Point(x as y)
    ("parameters", None),                 # def f(x, *, y)  (covers nested typed/default params)
    ("typed_parameter", None),
    ("default_parameter", None),
    ("import_statement", None),           # import ...  (names introduced)
    ("import_from_statement", None),      # from ... import ...
}

def _field_name_in_parent(node):
    p = node.parent
    if p is None:
        return None
    # Find the child's index to ask the parent for the field name.
    for i in range(p.child_count):
        if p.child(i).id == node.id:
            return p.field_name_for_child(i)
    return None

class PythonAnalyzer(BaseLSPAnalyzer):
    def get_server_command(self): return ["pylsp"]
    def get_file_extensions(self): return [".py"]
    def get_language_id(self): return "python"

    def needs_did_open(self) -> bool:
        return True

    def get_max_concurrency(self) -> int:
        return 4  # keep initial pressure low

    def get_warmup_seconds(self) -> float:
        return 120.0

    def get_initialize_options(self) -> dict:
        # Tweak for pyright: enable indexing to improve definition perf
        return {"python": {"analysis": {"indexing": True}}}

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

            # Exclude any identifier that sits in a binding slot (parent, field)
            field = _field_name_in_parent(node)
            # if (p.type, field) in BINDING_SLOTS or (p.type, None) in BINDING_SLOTS:
            #     continue

            # Also skip keyword_identifier nodes (rare edge in some grammars)
            if p.type == "keyword_identifier":
                continue

            # At this point we consider it a "read"
            out.append((node.start_point[0], node.start_point[1]))

        return out
