from pathlib import Path
from typing import List, Tuple, Dict
import logging
import os

from ..core.base_analyser import BaseLSPAnalyzer

logger = logging.getLogger(__name__)

QUERY = r"""
(call_expression
    function: (identifier) @name) @reference.call

(call_expression
    function: (field_expression
        field: (field_identifier) @name)) @reference.call

(macro_invocation
    macro: (identifier) @name) @reference.call

(impl_item
    trait: (type_identifier) @name) @reference.implementation

(impl_item
    type: (type_identifier) @name
    !trait) @reference.implementation
"""

# tree-sitter via tree_sitter_languages (recommended)
try:
    from tree_sitter_languages import get_parser, get_language
    LANG_RUST = get_language("rust")
    PARSER = get_parser("rust")
    QUERY_OBJ = LANG_RUST.query(QUERY)
except Exception as e:
    logger.warning("tree-sitter Rust not available: %s", e)
    LANG_RUST, PARSER, QUERY_OBJ = None, None, None


class RustAnalyzer(BaseLSPAnalyzer):
    def __init__(self, repo_path: Path, show_progress: bool = True):
        super().__init__(repo_path, show_progress)
        if LANG_RUST is None or PARSER is None or QUERY_OBJ is None:
            logger.warning(
                "Rust tree-sitter setup missing. Install `tree_sitter_languages` "
                "so references can be extracted."
            )
        self._discovered: Dict[str, List[Path]] = {
            "cargo_tomls": [],
            "crate_roots": [],
            "fallback_roots": [],
        }

    # ---- LSP server command (rust-analyzer) ----
    def get_server_command(self) -> List[str]:
        # Requires `rust-analyzer` on PATH
        return ["rust-analyzer"]

    def get_file_extensions(self) -> List[str]:
        return [".rs"]

    def get_language_id(self) -> str:
        return "rust"

    # rust-analyzer indexes workspaces/crates; didOpen per-file is not required
    def needs_did_open(self) -> bool:
        return False

    def get_warmup_seconds(self) -> float:
        return 120.0

    def get_max_concurrency(self) -> int:
        return min(16, max(6, (os.cpu_count() or 8)))
    
    def is_excluded_definition_path(self, path: Path) -> bool:
        parts = set(path.parts)
        exclude = {
            "target", ".git",
        }
        return not parts.isdisjoint(exclude)

    # ---------- Adaptive project discovery ----------
    def _skip_dir(self, p: Path) -> bool:
        skip = {
            ".git", "target", "node_modules", "vendor", ".venv", "venv",
            "build", "dist", ".tox", ".eggs"
        }
        return any(s in p.parts for s in skip)

    def _discover_crates(self):
        """Find all Cargo.toml files and their parent dirs (crate roots)."""
        cargo_tomls: List[Path] = []
        for toml in self.repo_path.rglob("Cargo.toml"):
            if not self._skip_dir(toml):
                cargo_tomls.append(toml.resolve())

        crate_roots = sorted({t.parent for t in cargo_tomls})
        self._discovered["cargo_tomls"] = cargo_tomls
        self._discovered["crate_roots"] = crate_roots

    def _discover_fallback_roots(self):
        """If no Cargo.toml, fall back to parent dirs of .rs files."""
        rs_parents = set()
        for f in self.repo_path.rglob("*.rs"):
            if not self._skip_dir(f):
                rs_parents.add(f.parent.resolve())
        self._discovered["fallback_roots"] = sorted(rs_parents)

    def get_initialize_options(self) -> dict:
        # Run discovery once (idempotent if called multiple times)
        if not self._discovered["cargo_tomls"]:
            self._discover_crates()
        if not self._discovered["cargo_tomls"] and not self._discovered["fallback_roots"]:
            self._discover_fallback_roots()

        init_opts = {}

        # Prefer explicit cargo projects via linkedProjects (best results)
        if self._discovered["cargo_tomls"]:
            linked = [str(p) for p in self._discovered["cargo_tomls"]]
            init_opts["rust-analyzer"] = {"linkedProjects": linked}

        # You could add more rust-analyzer options here if desired, e.g.:
        # init_opts.setdefault("rust-analyzer", {}).update({
        #     "cargo": {"loadOutDirsFromCheck": True}
        # })

        return init_opts

    async def start_server(self):
        """
        Use base startup (which calls initialize with get_initialize_options()),
        then dynamically add all crate/fallback roots as workspace folders.
        """
        # Ensure discovery before base start
        self._discover_crates()
        if not self._discovered["cargo_tomls"]:
            self._discover_fallback_roots()

        await super().start_server()

        # Build workspace folder adds (avoid duplicating the repo root)
        adds: List[Dict] = []
        roots = (
            self._discovered["crate_roots"]
            if self._discovered["cargo_tomls"]
            else self._discovered["fallback_roots"]
        )

        for root in roots:
            # Skip if it's exactly the repo root
            try:
                root.relative_to(self.repo_path)
            except ValueError:
                # outside repo — skip
                continue
            if root == self.repo_path:
                continue
            adds.append({"uri": root.as_uri(), "name": root.name})

        if adds:
            await self.client.send_notification(
                "workspace/didChangeWorkspaceFolders",
                {"event": {"added": adds, "removed": []}},
            )

    # ---------- Reference position extraction via tree-sitter ----------
    def ref_pos_extractor(self, text: str, path: Path) -> List[Tuple[int, int]]:
        if PARSER is None or QUERY_OBJ is None:
            return []

        tree = PARSER.parse(text.encode("utf-8"))
        captures = QUERY_OBJ.captures(tree.root_node)

        positions: List[Tuple[int, int]] = []
        seen = set()
        for node, cap_name in captures:
            if cap_name == "name":
                row, col = node.start_point  # 0-based positions
                key = (row, col)
                if key not in seen:
                    seen.add(key)
                    positions.append(key)
        return positions
