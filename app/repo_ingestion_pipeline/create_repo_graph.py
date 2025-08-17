"""
Stage 4: AST Processing
Creates AST for code files and builds graph in Neo4j.
"""

import uuid
from pathlib import Path
from typing import Any, List
import tree_sitter as ts
from tree_sitter_languages import get_parser, get_language
import traceback

from .core.base import PipelineStage, StageResult
from ..models.data_model import (
    S3StorageInfo, ReferenceResolutionResult, CodeFile, 
    ASTNode, CodeGraph
)
from ..db import get_neo4j_client


class ASTProcessorStage(PipelineStage):
    """Stage for AST creation and graph database population."""
    
    def __init__(self, config: dict = None):
        super().__init__("AST Processing", config)
        self.supported_languages = config.get("supported_languages", ["python", "javascript", "typescript"])
        self.neo4j_client = get_neo4j_client()
    
        self.max_file_size = config.get("max_file_size", 1024 * 1024)  # 1MB
        self.job_id = config.get("job_id", "unknown")
    
    async def execute(self, input_data: dict) -> StageResult:
        """
        Process AST and create graph in Neo4j.
        
        Args:
            input_data: Dict with s3_info and reference_results
            
        Returns:
            StageResult with CodeGraph data
        """
        try:
            s3_info: S3StorageInfo = input_data["s3_info"]
            reference_results: ReferenceResolutionResult = input_data["reference_results"]
            # print(f"Job {self.job_id}: Resolved references: {reference_results.references}")
            repo_path = s3_info.local_path
            repo_id = s3_info.repo_info.repo_id
            
            print(f"Job {self.job_id}: Starting AST processing for repository: {repo_id}")
            
            # Discover code files
            code_files = await self._discover_code_files(repo_path)
            print(f"Job {self.job_id}: Found {len(code_files)} code files to process")
            
            # Process each file and create AST
            # Create shared lookup dictionary
            global_leaf_lookup = {}
            all_nodes = []
            all_edges = []

            # First pass: Process all files to build AST and populate global lookup
            for idx, file in enumerate(code_files, 1):
                # print(f"Job {self.job_id}: Processing file {idx}/{len(code_files)}: {file.relative_path}")
                nodes, edges = await self._process_file_ast(file, repo_id, global_leaf_lookup)
                all_nodes.extend(nodes)
                all_edges.extend(edges)

            # print(f"Job {self.job_id}: Gloabal leaf created: {global_leaf_lookup}")

            reference_edges = self._create_reference_edges(all_nodes, reference_results.references, global_leaf_lookup)
            all_edges.extend(reference_edges)
            print(f"Job {self.job_id}: Created {len(reference_edges)} reference edges")
            
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
            
            print(
                f"Job {self.job_id}: AST processing completed: {len(all_nodes)} nodes, "
                f"{len(all_edges)} edges created"
            )
            
            return StageResult(
                success=True,
                data={
                    "s3_info": s3_info,
                    "reference_results": reference_results,
                    "repo_name": repo_id,
                },
                metadata={
                    "stage": self.name,
                    "files_processed": len(code_files),
                    "total_nodes": len(all_nodes),
                    "total_edges": len(all_edges)
                }
            )
            
        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: AST processing error: {str(e)}")
            return StageResult(
                success=False,
                data=None,
                metadata={"stage": self.name},
                error=str(e)
            )
    
    async def _discover_code_files(self, repo_path: Path) -> List[CodeFile]:
        """Discover and classify code files in the repository."""
        code_files = []
        
        # File extension to language mapping
        lang_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".jsx": "javascript",
            ".tsx": "typescript"
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
            
            # print(f"Job {self.job_id}: Processed {code_file.relative_path}: {len(nodes)} nodes")
            return nodes, edges
            
        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: Failed to process AST for {code_file.relative_path}: {str(e)}")
            return [], []
    
    async def _store_in_neo4j(self, code_graph: CodeGraph):
        """Store the code graph in Neo4j database."""
        try:
            print(f"Job {self.job_id}: Storing graph in Neo4j: {code_graph.total_nodes} nodes, {code_graph.total_edges} edges")
            
            # TODO: Implement actual Neo4j storage
            # For now, simulate successful storage
            await self.neo4j_client.store_graph(code_graph)
            
            print(f"Job {self.job_id}: Graph successfully stored in Neo4j")
            
        except Exception as e:
            print(f"Job {self.job_id}: Failed to store graph in Neo4j: {str(e)}")
            raise
    
    def _create_reference_edges(self, nodes: List[ASTNode], references_dict: dict, global_leaf_lookup: dict) -> List[dict]:
        """Create edges between references and their definitions."""

        print(f"Job {self.job_id}: Creating reference edges from references dictionary")
        reference_edges = []
        nodes_by_id = {node.node_id: node for node in nodes}
        
        for ref_location, definitions in references_dict.items():
            if not definitions:
                continue
                
            # Get the last (most specific) definition
            for def_location in definitions:
                # def_location = definitions[-1]
                ref_file, ref_line, ref_col = ref_location
                def_file, def_line, def_col = def_location
                
                # Find reference node
                ref_node_id = self._find_node_at_location(ref_file, ref_line, ref_col, global_leaf_lookup)
                if not ref_node_id:
                    print(f"Job {self.job_id}: Could not find reference node at {ref_location}")
                    continue
                
                # Find definition node
                def_node_id = self._find_node_at_location(def_file, def_line, def_col, global_leaf_lookup)
                if not def_node_id:
                    print(f"Job {self.job_id}: Could not find definition node at {def_location}")
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