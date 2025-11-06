"""
Stage 4: AST Processing
Creates AST for code files and builds graph in Neo4j.
"""

import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple
import tree_sitter as ts
from tree_sitter_languages import get_parser, get_language
import traceback

from ..models.data_model import (
    ReferenceResolutionResult, CodeFile, 
    ASTNode, CodeGraph
)
from ..parse_dependencies import parse_dependencies
from ..db import get_neo4j_client

LANGUAGE_DEFINITION_MAP = {
    "python": {"function_definition", "class_definition"},
    "javascript": {
        "function_declaration", "class_declaration", "method_definition",
        "arrow_function", "generator_function", "variable_declarator"
    },
    "typescript": {
        "function_declaration", "class_declaration", "method_definition",
        "arrow_function", "variable_declarator",
        "interface_declaration", "type_alias_declaration",
        "enum_declaration", "abstract_class_declaration",
        "module_declaration"
    },
    "java": {
        "class_declaration", "interface_declaration", "enum_declaration",
        "annotation_type_declaration", "method_declaration",
        "constructor_declaration", "field_declaration",
        "record_declaration"
    }
}


class ASTProcessorStage():
    """Stage for AST creation and graph database population."""
    
    def __init__(self, config: dict = None):
        self.supported_languages = config.get("supported_languages", ["python", "javascript", "typescript", "java"])
        self.neo4j_client = get_neo4j_client()
    
        self.max_file_size = config.get("max_file_size", 1024 * 1024)  # 1MB
        self.job_id = config.get("job_id", "unknown")
        self.language_defs = LANGUAGE_DEFINITION_MAP

    
    async def run(self, local_path: Path, repo_id: str, reference_results: List[dict]) -> None:
        """
        Process AST and create graph in Neo4j.
        
        Args:
            input_data: Dict with s3_info and reference_results
            
        Returns:
            StageResult with CodeGraph data
        """
        try:
            repo_path = local_path
            
            print(f"Job {self.job_id}: Starting AST processing for repository: {repo_id}")
            
            # Discover code files
            code_files = await self._discover_code_files(repo_path)
            # print(f"Job {self.job_id}: Found {len(code_files)} code files to process")

            # Resolve file dependencies
            # code_file_dependencies: Dict[str, List[str]] = parse_dependencies(repo_path)
            # file_nodes, file_edges = await self._process_file_deps(code_file_dependencies)

            # Process each file and create AST
            # Create shared lookup dictionary
            global_leaf_lookup = {}
            all_nodes = []
            all_edges = []

            # First pass: Process all files to build AST and populate global lookup
            for idx, file in enumerate(code_files, 1):
                nodes, edges = await self._process_file_ast(file, repo_id, global_leaf_lookup)
                all_nodes.extend(nodes)
                all_edges.extend(edges)

            # # print(f"Job {self.job_id}: Gloabal leaf created: {global_leaf_lookup}")

            reference_edges = self._create_reference_edges(all_nodes, reference_results, global_leaf_lookup)
            all_edges.extend(reference_edges)

            # print(
            #     f"Job {self.job_id}: Pre-Pruning: {len(all_nodes)} nodes, "
            #     f"{len(all_edges)} edges created"
            # )

            all_nodes, all_edges = self._prune_graph(all_nodes, all_edges)

            # all_nodes.extend(file_nodes)
            # all_edges.extend(file_edges)


            print(f"Job {self.job_id}: Created {len(all_edges)} edges")
            
            # Create graph representation
            code_graph = CodeGraph(
                repo_id=repo_id,
                nodes=all_nodes,
                edges=all_edges,
                total_nodes=len(all_nodes),
                total_edges=len(all_edges)
            )
            
            # Store in Neo4j
            await self._store_in_neo4j(code_graph)
            
        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: AST processing error: {str(e)}")
    
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
            if suffix not in lang_map:
                continue
            
            language = lang_map[suffix]
            if language not in self.supported_languages:
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
    
    # This only creates file-to-file dependency nodes and edges for code files where Stack graphs fail us.
    async def _process_file_deps(self, file_dependencies: Dict[str, List[str]]) -> Tuple[List[ASTNode], List[dict]]:
        """
        Build a lightweight AST/graph for file-to-file dependencies.

        Given a mapping of {source_file: [target_file, ...]}, this method:
        - Creates one ASTNode per unique file (node_type="file").
        - Creates directed edges from each source file to each target file.
        - Uses edge type "DEPENDS_ON" to distinguish from structural "CONTAINS" edges.

        Returns:
            (nodes, edges): A tuple where `nodes` is a list of ASTNode instances,
                            and `edges` is a list of edge dicts like in _process_file_ast.
        """
        nodes: List[ASTNode] = []
        edges: List[dict] = []

        if not file_dependencies:
            return nodes, edges

        # Collect all unique files (both sources and targets)
        unique_files: set[str] = set()
        for src, tgts in file_dependencies.items():
            if src:
                unique_files.add(src)
            for tgt in tgts or []:
                if tgt:
                    unique_files.add(tgt)

        # Create a node per unique file
        file_node_map: Dict[str, str] = {}  # file_path -> node_id
        for file_path in sorted(unique_files):
            # Construct a stable node_id. We don't have repo_id here, so namespace with job_id and "scala"
            node_id = f"{self.job_id}:scala:{file_path}:0:0:file"

            node = ASTNode(
                node_id=node_id,
                node_type="file",
                start_line=0,
                start_column=0,
                end_line=0,
                end_column=0,
                parent_id=None,
                children_ids=[],
                is_definition=True,
                file_path=str(file_path),
            )
            # Optional metadata, in line with how _process_file_ast adds dynamic fields
            node.name = str(file_path)

            nodes.append(node)
            file_node_map[file_path] = node_id

        # Create dependency edges: source_file --DEPENDS_ON--> target_file
        for src, tgts in file_dependencies.items():
            if not src or src not in file_node_map:
                continue

            src_id = file_node_map[src]
            sequence = 1
            for tgt in tgts or []:
                tgt_id = file_node_map.get(tgt)
                if not tgt_id:
                    continue

                edges.append({
                    "source": src_id,
                    "target": tgt_id,
                    "type": "DEPENDS_ON",
                    "sequence": sequence,
                })
                sequence += 1

        return nodes, edges

    async def _process_file_ast(self, code_file: CodeFile, repo_id: str, global_leaf_lookup: dict = None) -> tuple[List[ASTNode], List[dict]]:
        """Process a single file and create AST nodes and edges."""
        try:
            # Get parser for the language
            # print(f"Job {self.job_id}: Processing AST for file: {code_file.relative_path} ({code_file.language})")
            parser = get_parser(code_file.language)
            
            # Parse the code
            tree = parser.parse(bytes(code_file.content, "utf8"))
            
            # BFS traversal to create nodes and edges
            nodes = []
            edges = []
            leaf_nodes = []
            node_queue = [(tree.root_node, None, 1)]  # (node, parent_id)

            while node_queue:
                current_node, parent_id, edge_seq = node_queue.pop(0)
                
                # Create unique node ID
                node_id = f"{repo_id}:{code_file.relative_path}:{current_node.start_point[0]}:{current_node.start_point[1]}:{current_node.type}"
                
                # Create AST node
                ast_node = ASTNode(
                    node_id=node_id,
                    node_type=current_node.type,
                    start_line=current_node.start_point[0],
                    start_column=current_node.start_point[1],
                    end_line=current_node.end_point[0],
                    end_column=current_node.end_point[1],
                    parent_id=parent_id,
                    children_ids=[],
                    is_definition = current_node.type in self.language_defs.get(code_file.language, set()),
                    file_path=str(code_file.relative_path),
                )
                
                nodes.append(ast_node)
                
                # Check if this is a leaf node and add to global lookup
                is_leaf = len(current_node.children) == 0 or all(not child.is_named for child in current_node.children)
                
                # Get the code content for the current_node
                if is_leaf:
                    leaf_nodes.append(ast_node)
                    ast_node.is_reference = True  # Mark as reference node
                    start_byte = current_node.start_byte
                    end_byte = current_node.end_byte
                    node_content = code_file.content[start_byte:end_byte]
                    ast_node.name = node_content
                    if global_leaf_lookup is not None:
                        # Add to global lookup for coordinate matching
                        lookup_key = (
                            str(code_file.relative_path),
                            ast_node.start_line,
                            ast_node.start_column,
                            ast_node.end_line,
                            ast_node.end_column,

                        )
                        global_leaf_lookup[lookup_key] = ast_node.node_id
                
                # Create edge to parent if exists
                if parent_id:
                    edges.append({
                        "source": parent_id,
                        "target": node_id,    #current_node.id
                        "type": "CONTAINS",
                        "sequence": edge_seq,
                    })
                
                edge_sequence = 1
                # Add children to queue
                for child in current_node.children:
                    if child.is_named:
                        # current_node.id
                        node_queue.append((child, node_id, edge_sequence))
                        edge_sequence += 1
            
            return nodes, edges
            
        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: Failed to process AST for {code_file.relative_path}: {str(e)}")
            return [], []
    
    async def _store_in_neo4j(self, code_graph: CodeGraph):
        """Store the code graph in Neo4j database."""
        try:
            print(f"Job {self.job_id}: Storing graph in Neo4j: {code_graph.total_nodes} nodes, {code_graph.total_edges} edges")
            
            await self.neo4j_client.store_graph(code_graph)
            
            print(f"Job {self.job_id}: Graph successfully stored in Neo4j")
            
        except Exception as e:
            print(f"Job {self.job_id}: Failed to store graph in Neo4j: {str(e)}")
            raise
    
    def _create_reference_edges(self, nodes: List[ASTNode], references_list: List[Dict], global_leaf_lookup: dict) -> List[dict]:
        """Create edges between references and their definitions."""

        print(f"Job {self.job_id}: Creating reference edges from references dictionary")
        reference_edges = []
        nodes_by_id = {node.node_id: node for node in nodes}
        
        # Example references_list format:
        # [{
        #     "reference": {
        #     "file_path": "ann/src/main/scala/com/twitter/ann/scalding/offline/KnnTruthSetGenerator.scala",
        #     "line": 59,
        #     "column": 7,
        #     "location_string": "ann/src/main/scala/com/twitter/ann/scalding/offline/KnnTruthSetGenerator.scala:59:7"
        #     },
        #     "definitions": [
        #     {
        #         "file_path": "ann/src/main/scala/com/twitter/ann/scalding/offline/KnnHelper.scala",
        #         "line": 168,
        #         "column": 6,
        #         "location_string": "ann/src/main/scala/com/twitter/ann/scalding/offline/KnnHelper.scala:168:6"
        #     }
        #     ]
        # }]
        for ref_info in references_list:
            definitions = ref_info.get("definitions", [])
            if not definitions:
                continue
                
            # Get the last (most specific) definition
            # for def_location in definitions:

            ref_file = ref_info["reference"]["file_path"]
            ref_line = ref_info["reference"]["line"]
            ref_col = ref_info["reference"]["column"]

            def_location = definitions[0]
            def_file = def_location["file_path"]
            def_line = def_location["line"]
            def_col = def_location["column"]

            # Find reference node
            ref_node_id = self._find_node_at_location(ref_file, ref_line, ref_col, global_leaf_lookup)
            if not ref_node_id:
                # print(f"Job {self.job_id}: Could not find reference node at {ref_location}")
                continue
            
            # Find definition node
            def_node_id = self._find_node_at_location(def_file, def_line, def_col, global_leaf_lookup)
            if not def_node_id:
                # print(f"Job {self.job_id}: Could not find definition node at {def_location}")
                continue
            
            # Create reference edge
            reference_edges.append({
                "source": ref_node_id,
                "target": def_node_id,
                "type": "REFERENCES",
                "sequence": 1,
            })
            
            # Mark definition node based on parent's child count
            if def_node_id in nodes_by_id:
                def_node = nodes_by_id[def_node_id]
                if def_node.parent_id and def_node.parent_id in nodes_by_id:
                    parent_node = nodes_by_id[def_node.parent_id]
                    parent_node.is_definition = True
                    # Read file content from def_node's file_path
                    file_path = Path(def_node.file_path)
                    try:
                        with file_path.open('r', encoding='utf-8', errors='ignore') as f:
                            lines = f.readlines()
                            # Extract content based on line and column numbers
                            if def_node.start_line == def_node.end_line:
                                # Single line case
                                line_content = lines[def_node.start_line]
                                node_content = line_content[def_node.start_column:def_node.end_column]
                            else:
                                # Multi-line case
                                node_content = []
                                for i, line in enumerate(lines[def_node.start_line:def_node.end_line + 1]):
                                    if i == 0:
                                        # First line, start from start_column
                                        node_content.append(line[def_node.start_column:])
                                    elif i == def_node.end_line - def_node.start_line:
                                        # Last line, end at end_column
                                        node_content.append(line[:def_node.end_column])
                                    else:
                                        # Middle lines, include full line
                                        node_content.append(line)
                                node_content = ''.join(node_content)
                            parent_node.name = node_content.strip()
                    except Exception as e:
                        print(f"Job {self.job_id}: Failed to read file {file_path} for def_node content: {str(e)}")

        return reference_edges

    def _find_node_at_location(self, file_path: str, line: int, col: int, global_leaf_lookup: dict) -> str:
        # print(f"Job {self.job_id}: Finding node at {file_path}:{line}:{col}")
        """Find the AST node ID at the given location."""
        for (lookup_file, start_line, start_col, end_line, end_col), node_id in global_leaf_lookup.items():
            if (lookup_file == file_path and 
                (line, col) >= (start_line, start_col) and 
                (line, col) <= (end_line, end_col)):
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
            # We'll also start separate traversals from any kept nodes later.
            first = next(iter(all_ids), None)
            if first is not None:
                root_ids = {first}

        # Decide which nodes to keep
        def is_kept(n: ASTNode) -> bool:
            return (n.node_id in root_ids) or getattr(n, "is_definition", False) or getattr(n, "is_reference", False)

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

                # If we've fully processed this child elsewhere and it's not needed to re-walk, skip
                # (We still attached above if necessary.)
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

