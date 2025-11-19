"""
Stage 4: AST Processing
Creates AST for code files and builds graph in Neo4j.
"""

import uuid
import sqlite3
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
from collections import deque
import tree_sitter as ts
from tree_sitter_languages import get_parser, get_language
import traceback

from ..models.data_model import (
    ReferenceResolutionResult, CodeFile,
    ASTNode, CodeGraph
)
from ..db import get_neo4j_client

try:
    from tqdm import tqdm
except Exception:
    tqdm = None

LANGUAGE_DEFINITION_MAP = {
    "python": {"function_definition", "class_definition", "assignment"},
    "rust": {
        "struct_item", "enum_item", "union_item", "type_item",
        "function_item", "trait_item", "mod_item", "macro_definition"
    },
    "scala": {
        "package_clause", "trait_definition", "enum_definition",
        "simple_enum_case", "full_enum_case", "class_definition",
        "object_definition", "function_definition", "val_definition",
        "given_definition", "var_definition", "val_declaration",
        "var_declaration", "type_definition", "class_parameter"
    },
    "java": {
        "class_declaration", "method_declaration", "interface_declaration"
    }
}


class ASTProcessorStage():
    """Stage for AST creation and graph database population."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        # We now treat lang_map as the source of truth; supported_languages is mostly legacy
        self.neo4j_client = get_neo4j_client()

        self.max_file_size = self.config.get("max_file_size", 1024 * 1024)  # 1MB
        self.job_id = self.config.get("job_id", "unknown")
        self.language_defs = LANGUAGE_DEFINITION_MAP

        # Parser cache: language -> parser
        self._parsers: Dict[str, Any] = {}

        # File content cache for definition-name extraction
        self._file_content_cache: Dict[str, List[str]] = {}

    async def run(self, local_path: Path, repo_id: str) -> None:
        """
        Process AST and create graph in Neo4j.

        Args:
            local_path: path to local repo checkout
            repo_id: repo identifier
        """
        conn = None
        try:
            repo_path = local_path

            print(f"Job {self.job_id}: Starting AST processing for repository: {repo_id}")

            # Clear existing graph state for this repo
            await self.neo4j_client.init_graph_for_repo(repo_id)

            # Discover code files
            code_files = await self._discover_code_files(repo_path)
            total_files = len(code_files)
            print(f"Job {self.job_id}: Discovered {total_files} code files")

            # Open SQLite cache (if present)
            db_path = repo_path / ".lsp_ref_cache.sqlite"
            conn = sqlite3.connect(db_path) if db_path.exists() else None
            cursor = conn.cursor() if conn else None

            # Global leaf lookup:
            #   file_path -> List of (start_line, start_col, end_line, end_col, node_id)
            global_leaf_lookup: Dict[str, List[Tuple[int, int, int, int, str]]] = {}
            all_nodes: List[ASTNode] = []
            all_edges: List[dict] = []

            # First pass: Process all files to build AST and populate global lookup
            file_iter = code_files
            file_bar = None
            if tqdm is not None:
                file_bar = tqdm(total=total_files, desc="Building ASTs", leave=True)

            for idx, code_file in enumerate(file_iter, 1):
                nodes, edges = await self._process_file_ast(
                    code_file, repo_id, global_leaf_lookup
                )
                all_nodes.extend(nodes)
                all_edges.extend(edges)

                if file_bar:
                    file_bar.update(1)

            if file_bar:
                file_bar.close()

            # Second pass: For each file, fetch its reference mappings from SQLite and build reference edges
            if conn:
                print(f"Job {self.job_id}: Creating reference edges from SQLite cache")
                # We could filter files to only those that appear in mappings, but you
                # explicitly want ASTs for every file, so we just iterate all.
                ref_bar = None
                if tqdm is not None:
                    ref_bar = tqdm(total=len(code_files), desc="Attaching references", leave=True)

                for code_file in code_files:
                    ref_path = str(code_file.relative_path)  # must match reference["file_path"]
                    cursor.execute(
                        "SELECT data FROM mappings WHERE ref_path = ?",
                        (ref_path,),
                    )
                    rows = cursor.fetchall()
                    if rows:
                        refs_for_file = [json.loads(row[0]) for row in rows]
                        reference_edges = self._create_reference_edges(
                            all_nodes, refs_for_file, global_leaf_lookup
                        )
                        all_edges.extend(reference_edges)
                    if ref_bar:
                        ref_bar.update(1)

                if ref_bar:
                    ref_bar.close()

            print(
                f"Job {self.job_id}: Pre-Pruning: {len(all_nodes)} nodes, "
                f"{len(all_edges)} edges created"
            )

            all_nodes, all_edges = self._prune_graph(all_nodes, all_edges)

            print(f"Job {self.job_id}: After pruning: {len(all_nodes)} nodes, {len(all_edges)} edges")

            # --- Batched writes to Neo4j with progress bars ---
            # Nodes
            if tqdm is not None:
                nodes_bar = tqdm(total=len(all_nodes), desc="Writing nodes to Neo4j", leave=True)
            else:
                nodes_bar = None

            for chunk in self._chunk_list(all_nodes, 5000):
                # neo4j_client handles the details internally
                await self.neo4j_client._batch_create_nodes(chunk, repo_id)
                if nodes_bar:
                    nodes_bar.update(len(chunk))

            if nodes_bar:
                nodes_bar.close()

            # Edges
            if tqdm is not None:
                edges_bar = tqdm(total=len(all_edges), desc="Writing edges to Neo4j", leave=True)
            else:
                edges_bar = None

            for chunk in self._chunk_list(all_edges, 5000):
                await self.neo4j_client._batch_create_edges(chunk, repo_id)
                if edges_bar:
                    edges_bar.update(len(chunk))

            if edges_bar:
                edges_bar.close()

            print(f"Job {self.job_id}: Finished writing graph to Neo4j")

        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: AST processing error: {str(e)}")
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _chunk_list(items: List[Any], chunk_size: int) -> List[List[Any]]:
        for i in range(0, len(items), chunk_size):
            yield items[i:i + chunk_size]

    async def _discover_code_files(self, repo_path: Path) -> List[CodeFile]:
        """Discover and classify code files in the repository."""
        code_files = []

        # File extension to language mapping
        lang_map = {
            ".py": "python",
            ".java": "java",
            ".scala": "scala",
            ".rs": "rust",
        }

        for file_path in repo_path.rglob("*"):
            if not file_path.is_file():
                continue

            suffix = file_path.suffix.lower()
            language = lang_map.get(suffix)
            # Only process languages we know about in lang_map
            if not language:
                continue

            # Check if the file is in a test directory
            if "tests" in file_path.parts or "test" in file_path.parts:
                continue

            try:
                # Check file size
                if file_path.stat().st_size > self.max_file_size:
                    print(f"Job {self.job_id}: Skipping large file: {file_path}")
                    continue

                # Read file content
                content = file_path.read_text(encoding='utf-8', errors='ignore')

                code_file = CodeFile(
                    file_path=file_path,
                    relative_path=file_path,
                    language=language,
                    content=content,
                    size=len(content)
                )

                code_files.append(code_file)

            except Exception as e:
                print(f"Job {self.job_id}: Failed to read file {file_path}: {str(e)}")

        return code_files

    def _get_parser(self, language: str):
        """Get or create a tree-sitter parser for a language."""
        parser = self._parsers.get(language)
        if parser is None:
            parser = get_parser(language)
            self._parsers[language] = parser
        return parser

    async def _process_file_ast(self, code_file: CodeFile, repo_id: str,
                                global_leaf_lookup: dict) -> tuple[List[ASTNode], List[dict]]:
        """Process a single file and create AST nodes and edges."""
        try:
            parser = self._get_parser(code_file.language)
            tree = parser.parse(bytes(code_file.content, "utf8"))

            nodes: List[ASTNode] = []
            edges: List[dict] = []

            # BFS queue using deque for O(1) pops
            node_queue = deque([(tree.root_node, None, 1)])  # (node, parent_id, sequence)

            file_key = str(code_file.relative_path)
            if file_key not in global_leaf_lookup:
                global_leaf_lookup[file_key] = []

            while node_queue:
                current_node, parent_id, edge_seq = node_queue.popleft()

                node_id = f"{repo_id}:{code_file.relative_path}:{current_node.start_point[0]}:{current_node.start_point[1]}:{current_node.type}"

                ast_node = ASTNode(
                    node_id=node_id,
                    node_type=current_node.type,
                    start_line=current_node.start_point[0],
                    start_column=current_node.start_point[1],
                    end_line=current_node.end_point[0],
                    end_column=current_node.end_point[1],
                    parent_id=parent_id,
                    children_ids=[],
                    is_definition=current_node.type in self.language_defs.get(code_file.language, set()),
                    file_path=file_key,
                )

                nodes.append(ast_node)

                is_leaf = (
                    len(current_node.children) == 0 or
                    all(not child.is_named for child in current_node.children)
                )

                if is_leaf:
                    ast_node.is_reference = True
                    start_byte = current_node.start_byte
                    end_byte = current_node.end_byte
                    node_content = code_file.content[start_byte:end_byte]
                    ast_node.name = node_content

                    global_leaf_lookup[file_key].append(
                        (
                            ast_node.start_line,
                            ast_node.start_column,
                            ast_node.end_line,
                            ast_node.end_column,
                            ast_node.node_id,
                        )
                    )

                if parent_id:
                    edges.append({
                        "source": parent_id,
                        "target": node_id,
                        "type": "CONTAINS",
                        "sequence": edge_seq,
                    })

                edge_sequence = 1
                for child in current_node.children:
                    if child.is_named:
                        node_queue.append((child, node_id, edge_sequence))
                        edge_sequence += 1

            return nodes, edges

        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: Failed to process AST for {code_file.relative_path}: {str(e)}")
            return [], []

    def _get_file_lines(self, file_path: str) -> List[str]:
        """Get file lines with a small in-memory cache to avoid re-reading."""
        if file_path in self._file_content_cache:
            return self._file_content_cache[file_path]
        p = Path(file_path)
        try:
            with p.open('r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            self._file_content_cache[file_path] = lines
            return lines
        except Exception:
            return []

    def _create_reference_edges(self, nodes: List[ASTNode], references_list: List[Dict],
                                global_leaf_lookup: dict) -> List[dict]:
        """Create edges between references and their definitions."""

        reference_edges: List[dict] = []
        nodes_by_id = {node.node_id: node for node in nodes}

        for ref_info in references_list:
            definitions = ref_info.get("definitions", [])
            if not definitions:
                continue

            ref_file = ref_info["reference"]["file_path"]
            ref_line = ref_info["reference"]["line"]
            ref_col = ref_info["reference"]["column"]

            def_location = definitions[0]
            def_file = def_location["file_path"]
            def_line = def_location["line"]
            def_col = def_location["column"]

            ref_node_id = self._find_node_at_location(ref_file, ref_line, ref_col, global_leaf_lookup)
            if not ref_node_id:
                continue

            def_node_id = self._find_node_at_location(def_file, def_line, def_col, global_leaf_lookup)
            if not def_node_id:
                continue

            reference_edges.append({
                "source": ref_node_id,
                "target": def_node_id,
                "type": "REFERENCES",
                "sequence": 1,
            })

            if def_node_id in nodes_by_id:
                def_node = nodes_by_id[def_node_id]
                if def_node.parent_id and def_node.parent_id in nodes_by_id:
                    parent_node = nodes_by_id[def_node.parent_id]
                    parent_node.is_definition = True
                    file_path = parent_node.file_path or def_node.file_path
                    lines = self._get_file_lines(file_path)
                    try:
                        if lines:
                            if def_node.start_line == def_node.end_line:
                                line_content = lines[def_node.start_line]
                                node_content = line_content[def_node.start_column:def_node.end_column]
                            else:
                                node_content_parts = []
                                for i, line in enumerate(lines[def_node.start_line:def_node.end_line + 1]):
                                    if i == 0:
                                        node_content_parts.append(line[def_node.start_column:])
                                    elif i == def_node.end_line - def_node.start_line:
                                        node_content_parts.append(line[:def_node.end_column])
                                    else:
                                        node_content_parts.append(line)
                                node_content = ''.join(node_content_parts)
                            parent_node.name = node_content.strip()
                    except Exception as e:
                        print(f"Job {self.job_id}: Failed to extract content for def_node: {str(e)}")

        return reference_edges

    def _find_node_at_location(self, file_path: str, line: int, col: int,
                               global_leaf_lookup: dict) -> str:
        """Find the AST node ID at the given location (per-file scan)."""
        for (start_line, start_col, end_line, end_col, node_id) in global_leaf_lookup.get(file_path, []):
            if (line, col) >= (start_line, start_col) and (line, col) <= (end_line, end_col):
                return node_id
        return None

    def validate_config(self) -> bool:
        """Validate stage configuration."""
        return (
            isinstance(self.config.get("supported_languages", []), list) and
            isinstance(self.config.get("max_file_size", 0), int)
        )

    def _prune_graph(self, nodes: List[ASTNode], edges: List[dict]) -> tuple[List[ASTNode], List[dict]]:
        """
        Prune non-root, non-definition, non-reference nodes while preserving hierarchy.

        This version is **iterative** (no recursion) and guards against accidental
        cycles in the CONTAINS graph to avoid RecursionError.
        """
        # Split edges by type
        contains_edges = [e for e in edges if e.get("type") == "CONTAINS"]
        ref_edges = [e for e in edges if e.get("type") == "REFERENCES"]

        nodes_by_id = {n.node_id: n for n in nodes}
        all_ids = set(nodes_by_id.keys())

        # Build children map (preserve original order via sequence) and indegree map
        children_seq: dict[str, List[tuple[int, str]]] = {}
        indegree: dict[str, int] = {}

        for e in contains_edges:
            src = e["source"]
            tgt = e["target"]
            seq = e.get("sequence", 1)

            children_seq.setdefault(src, []).append((seq, tgt))
            indegree[tgt] = indegree.get(tgt, 0) + 1
            indegree.setdefault(src, indegree.get(src, 0))

        # Sort children by the original sequence index and flatten to just child ids
        children: dict[str, List[str]] = {
            src: [cid for _, cid in sorted(lst, key=lambda x: x[0])]
            for src, lst in children_seq.items()
        }

        # Determine roots: nodes without a parent_id OR with indegree 0 from edges
        root_ids = {n.node_id for n in nodes if n.parent_id is None} | {
            nid for nid in all_ids if indegree.get(nid, 0) == 0
        }
        if not root_ids:
            # In pathological cases (cycle-only graphs), pick a stable pseudo-root
            # to ensure we still produce a connected pruned graph.
            first = next(iter(all_ids), None)
            if first is not None:
                root_ids = {first}

        # Decide which nodes to keep
        def is_kept(n: ASTNode) -> bool:
            return (
                (n.node_id in root_ids)
                or getattr(n, "is_definition", False)
                or getattr(n, "is_reference", False)
            )

        keep_ids = {n.node_id for n in nodes if is_kept(n)}

        # We'll rebuild CONTAINS edges, attaching each kept node to its nearest kept ancestor.
        new_contains: List[dict] = []
        next_seq: dict[str, int] = {}  # sequence counter per kept parent
        seen_attach: set[tuple[str, str]] = set()  # avoid duplicate (parent, child) edges

        def attach_edge(parent_id: str, child_id: str):
            if (parent_id, child_id) in seen_attach:
                return
            seq = next_seq.get(parent_id, 1)
            new_contains.append({
                "source": parent_id,
                "target": child_id,
                "type": "CONTAINS",
                "sequence": seq,
            })
            next_seq[parent_id] = seq + 1
            seen_attach.add((parent_id, child_id))

        visited: set[str] = set()

        def traverse(start_id: str, force_keep_root: bool = True):
            # Ensure the traversal start is kept (mirrors previous behavior)
            if force_keep_root:
                keep_ids.add(start_id)

            # Stack holds frames: (current_node_id, anchor_kept_id, next_child_index)
            stack: List[tuple[str, str | None, int]] = [(start_id, start_id, 0)]
            on_path: set[str] = {start_id}

            while stack:
                node_id, anchor, idx = stack.pop()
                child_list = children.get(node_id, [])

                if idx >= len(child_list):
                    visited.add(node_id)
                    on_path.discard(node_id)
                    continue

                # Re-push current frame with advanced child index
                stack.append((node_id, anchor, idx + 1))

                child_id = child_list[idx]

                # Skip back-edges to avoid cycles
                if child_id in on_path:
                    continue

                # Attach kept child to current anchor
                child_kept = child_id in keep_ids
                if child_kept and anchor is not None:
                    attach_edge(anchor, child_id)

                # The new anchor is the kept child, otherwise it stays the same
                new_anchor = child_id if child_kept else anchor

                if child_id in visited:
                    continue

                # Dive into child
                stack.append((child_id, new_anchor, 0))
                on_path.add(child_id)

        # Primary traversals from roots
        for root_id in root_ids:
            if root_id in all_ids:
                traverse(root_id, force_keep_root=True)

        # Secondary traversals: ensure all kept nodes in disconnected components get anchored
        for nid in (keep_ids - visited):
            traverse(nid, force_keep_root=True)

        # Filter nodes to the kept set
        pruned_nodes = [nodes_by_id[nid] for nid in keep_ids if nid in nodes_by_id]

        # Keep only reference edges whose endpoints remain
        kept = keep_ids
        pruned_ref_edges = [e for e in ref_edges if e["source"] in kept and e["target"] in kept]

        pruned_edges = new_contains + pruned_ref_edges
        return pruned_nodes, pruned_edges
