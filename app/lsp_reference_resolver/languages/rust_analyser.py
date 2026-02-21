import asyncio
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..core.lsp_client import LSPClient
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
    def __init__(
        self,
        repo_path: Path,
        base_repo_path: str,
        show_progress: bool = True,
        should_abort: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(
            repo_path,
            base_repo_path,
            show_progress=show_progress,
            should_abort=should_abort,
        )
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
    
    def get_timeout_seconds(self) -> float:
        return 120.0
    
    def get_total_server_instances(self) -> int:
        return 1

    # rust-analyzer indexes workspaces/crates; didOpen per-file is not required
    def needs_did_open(self) -> bool:
        return False

    def get_max_concurrency(self) -> int:
        return 8

    # ---------- Adaptive project discovery ----------
    def _skip_dir(self, p: Path) -> bool:
        skip = {
            ".git", "target", "node_modules", "vendor", ".venv", "venv",
            "build", "dist", ".tox", ".eggs"
        }
        return any(s in p.parts for s in skip)

    def get_rust_root(self) -> Path:
        """Find common parent of all Cargo.toml files."""
        if not self._discovered["cargo_tomls"]:
            self._discover_crates()
        
        if self._discovered["crate_roots"]:
            roots = self._discovered["crate_roots"]
            common = roots[0]
            for root in roots[1:]:
                while common not in root.parents and common != root:
                    common = common.parent
            return common
        
        return self.repo_path

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

        return init_opts

    async def _wait_for_rust_ready(self, client: LSPClient, timeout_per_token: float = 60.0):
        """Wait for rust-analyzer to fully index."""
        seen_tokens = set()
        
        while True:
            token = await client.wait_for_next_work_done_token(timeout=5.0)
            if not token or token in seen_tokens:
                break
            
            seen_tokens.add(token)
            logger.info(f"Waiting for: {token}")
            success = await client.wait_for_work_done(token, timeout=timeout_per_token)
            if success:
                logger.info(f"Completed: {token}")
            else:
                logger.info(f"Timeout waiting for: {token}, continuing...")
        
        # Final probe
        logger.info("Probing for readiness...")
        files = self.get_files()
        if files:
            uri = files[0].as_uri()
            params = {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0}}
            for _ in range(10):
                res = await client.send_request("textDocument/definition", params, timeout=5.0)
                if res is not None and res != []:
                    logger.info("rust-analyzer ready")
                    return
                await asyncio.sleep(2.0)
        
        logger.warning("rust-analyzer may not be fully ready")

    async def start_server(self):
        self._discover_crates()

        if not self._discovered["cargo_tomls"]:
            self._discover_fallback_roots()
            logger.info(f"Fallback roots: {self._discovered['fallback_roots']}")

        rust_root = self.get_rust_root()
        logger.info(f"Using Rust root: {rust_root}")

        # Initialize servers with rust_root instead of repo_path
        num_servers = self.get_total_server_instances()
        
        for i in range(num_servers):
            client = LSPClient(self.get_server_command())
            await client.start()
            await client.initialize(
                rust_root.as_uri(),
                rust_root.name,
                initialize_options=self.get_initialize_options(),
            )
            self.server_list.append(client)
            
            if i == 0:
                await self._wait_for_rust_ready(client)

        # Add workspace folders for individual crates
        adds: List[Dict] = []
        roots = (
            self._discovered["crate_roots"]
            if self._discovered["cargo_tomls"]
            else self._discovered["fallback_roots"]
        )

        for root in roots:
            try:
                root.relative_to(rust_root)
            except ValueError:
                continue
            if root == rust_root:
                continue
            adds.append({"uri": root.as_uri(), "name": root.name})

        if adds:
            for client in self.server_list:
                await client.send_notification(
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
