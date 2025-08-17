"""
Stage: Chunk LLM Processing
Chunks code into ~50-line logical blocks using AST, processes with LLM, stores summaries with node links.
"""

import os
import asyncio
import time
from typing import Any, List, Dict
import traceback

from ..config import Config
from .core.base import PipelineStage, StageResult
from ..models.data_model import (
    S3StorageInfo
)
from ..llm import GroqLLM
from ..db import get_neo4j_client, Neo4jClient, get_mongo_client
# from tqdm import tqdm

from pathlib import Path
import tree_sitter as ts
from tree_sitter_languages import get_parser

class ChunkLLMProcessorStage(PipelineStage):
    """Stage for chunk-based LLM analysis."""
    
    def __init__(self, config: dict = None):
        super().__init__("Chunk LLM Processing", config)
        self.mongo_client = get_mongo_client()
        self.chunk_summaries_collection = self.mongo_client[Config.CHUNK_SUMMARIES_COLLECTION]
        self.chunk_summaries_collection.create_index("chunk_id", unique=True)
        self.llm_client = GroqLLM()
        self.chunk_size = 50  # Target lines per chunk
        self.batch_size = 20  # Chunks per LLM request
        self.job_id = config.get("job_id", "unknown")
        self.parser = get_parser("python")  # Tree-sitter parser
    
    async def execute(self, input_data: dict) -> StageResult:
        try:
            s3_info: S3StorageInfo = input_data["s3_info"]
            neo4j_client: Neo4jClient = get_neo4j_client()
            repo_path = s3_info.local_path
            repo_id = s3_info.repo_info.repo_id
            
            print(f"Job {self.job_id}: Starting chunk LLM processing for repo: {repo_id}")
            
            # Discover code files (reuse from AST stage or scan)
            code_files = await self._discover_code_files(neo4j_client, repo_id)
            
            # Process each file

            # process each file concurrently using asyncio
            if not code_files:
                print(f"Job {self.job_id}: No code files found for repo {repo_id}")
                return StageResult(success=True, data={"repo_id": repo_id, "num_files": 0}, metadata={"stage": self.name})

            await asyncio.gather(*(self._process_file(file_path, neo4j_client, repo_id) for file_path in code_files))

            print(f"Job {self.job_id}: Completed chunk processing: {len(code_files)} files")
            
            return StageResult(
                success=True,
                data={
                    "repo_id": repo_id, 
                    "num_files": len(code_files),
                    "s3_info": s3_info,
                    "repo_name": repo_id,
                    "neo4j_client": neo4j_client
                },
                metadata={
                    "stage": self.name
                }
            )
        
        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: Chunk processing error: {str(e)}")
            return StageResult(success=False, error=str(e))

    async def _discover_code_files(self, neo4j_client: Neo4jClient, repo_id: str) -> List[str]:
        """Retrieve distinct file paths from Neo4j."""
        #print(f"Job {self.job_id}: Fetching file paths")
        query = """
        MATCH (n:ASTNode {repo_id: $repo_id})
        RETURN DISTINCT n.file_path AS file_path
        """
        try:
            with neo4j_client.driver.session() as session:
                result = session.run(query, repo_id=repo_id)
                paths = [record["file_path"] for record in result]
                #print(f"Job {self.job_id}: Found {len(paths)} file paths")
                return paths
        except Exception as e:
            traceback.print_exc()
            #print(f"Job {self.job_id}: Failed to get file paths: {e}")
            return []

    async def _process_file(self, file_path: str, neo4j_client: Neo4jClient, repo_id: str):
        """Chunk file, process with LLM, store."""
        print(f"Job {self.job_id}: Processing file: {file_path}")
        
        # Read code
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
        
        # Parse fresh AST with tree-sitter
        tree = self.parser.parse(bytes(code, "utf8"))
        
        # Bulk-fetch reference node IDs for this file from Neo4j
        refs_set = await self._fetch_references_for_file(neo4j_client, repo_id, file_path)
        # refs_set = set()
        
        # Generate chunks from AST
        chunks = self._generate_chunks(tree.root_node, code.splitlines(), refs_set, repo_id, file_path)

        # print(f"Job {self.job_id}: Generated {len(chunks)} chunks for {file_path}")
        # print(f"CHUNKS: {chunks}")

        # Process chunks concurrently (up to 100 at a time)
        semaphore = asyncio.Semaphore(500)  # Limit concurrency to 100
        async def limited_dispatch(chunk):
            async with semaphore:
                await self._dispatch_llm_request(chunk, file_path, repo_id, neo4j_client)
        
        await asyncio.gather(*(limited_dispatch(chunk) for chunk in chunks))
                
        
        # Store chunk summaries (done in batch dispatch)

    async def _fetch_references_for_file(self, neo4j_client: Neo4jClient, repo_id: str, file_path: str) -> set:
        """Bulk-fetch reference node IDs in this file."""
        query = """
        MATCH (n:ASTNode {repo_id: $repo_id, file_path: $file_path})-[:REFERENCES]->(m:ASTNode)
        RETURN DISTINCT n.node_id AS ref_node_id
        """
        with neo4j_client.driver.session() as session:
            result = session.run(query, repo_id=repo_id, file_path=file_path)
            refs = {record["ref_node_id"] for record in result}
        print(f"Job {self.job_id}: Fetched {len(refs)} reference nodes for {file_path}")
        return refs

    def _generate_chunks(self, root_node: ts.Node, lines: List[str], refs_set: set, repo_id: str, file_path: str) -> List[Dict]:
        # print(f"Ref sets: {refs_set}")
        """Traverse AST to create ~50-line chunks."""
        chunks = []
        current_chunk = {"start_line": 0, "end_line": -1, "text": "", "nodes": [], "refs": []}
        line_count = 0
        
        def add_to_chunk(node: ts.Node):
            node_id = f"{repo_id}:{file_path}:{node.start_point[0]}:{node.start_point[1]}:{node.type}"
            nonlocal line_count, current_chunk  # line_count now as current_size
            current_size = current_chunk["end_line"] - current_chunk["start_line"] + 1 if current_chunk["end_line"] >= current_chunk["start_line"] else 0
            potential_end = max(current_chunk["end_line"], node.end_point[0])
            extension_lines = potential_end - current_chunk["end_line"] if current_chunk["end_line"] >= current_chunk["start_line"] else (node.end_point[0] - node.start_point[0] + 1)
            if current_size + extension_lines > self.chunk_size * 1.2:
                if current_size > 0 and current_chunk["end_line"] >= current_chunk["start_line"]:
                    current_chunk["text"] = "\n".join(lines[current_chunk["start_line"]:current_chunk["end_line"] + 1])
                    chunks.append(current_chunk)
                else:
                    print(f"Job {self.job_id}: Skipping invalid chunk during add for {file_path}")
                overlap = min(10, current_size)
                new_start = current_chunk["end_line"] - overlap + 1 if current_size > 0 else node.start_point[0]
                current_chunk = {"start_line": new_start, "end_line": new_start - 1, "text": "", "nodes": [], "refs": []}
                current_size = 0  # Reset
            # Add node
            current_chunk["end_line"] = max(current_chunk["end_line"], node.end_point[0])
            current_chunk["nodes"].append(node_id)
            if node_id in refs_set:
                current_chunk["refs"].append(node_id)
            # Update size (no += node_lines; it's span-based now)
            line_count = current_chunk["end_line"] - current_chunk["start_line"] + 1 if current_chunk["end_line"] >= current_chunk["start_line"] else 0
            # Recurse
            for child in node.children:
                if child.is_named:
                    add_to_chunk(child)
        
        # Start traversal from root children (top-level blocks)
        for child in root_node.children:
            add_to_chunk(child)
        
        # Add last chunk
        if line_count > 0:
            current_chunk["text"] = "\n".join(lines[current_chunk["start_line"]:current_chunk["end_line"] + 1])
            chunks.append(current_chunk)
        
        return chunks

    async def get_ref_info(self, chunk: Dict[str, Any], neo4j_client: Neo4jClient) -> str:
        if chunk["refs"]:
            query = """
            UNWIND $ref_ids AS ref_id
            MATCH (r:ASTNode {node_id: ref_id})-[:REFERENCES]->(d:ASTNode)
            OPTIONAL MATCH (actual_def:ASTNode {node_id: d.parent_id})
            RETURN ref_id,
                r.file_path AS ref_file, r.start_line AS ref_start_line, r.end_line AS ref_end_line, r.start_column AS ref_start_col, r.end_column AS ref_end_col,
                coalesce(actual_def.file_path, d.file_path) AS def_file, 
                coalesce(actual_def.start_line, d.start_line) AS def_start_line, 
                coalesce(actual_def.end_line, d.end_line) AS def_end_line, 
                coalesce(actual_def.start_column, d.start_column) AS def_start_col, 
                coalesce(actual_def.end_column, d.end_column) AS def_end_col
            """
            with neo4j_client.driver.session() as session:
                result = session.run(query, ref_ids=chunk["refs"])
                refs_map = {}
                for record in result:
                    refs_map[record['ref_id']] = {
                        'ref_file': record['ref_file'],
                        'ref_start_line': record['ref_start_line'],
                        'ref_end_line': record['ref_end_line'],
                        'ref_start_col': record['ref_start_col'],
                        'ref_end_col': record['ref_end_col'],
                        'def_file': record['def_file'],
                        'def_start_line': record['def_start_line'],
                        'def_end_line': record['def_end_line'],
                        'def_start_col': record['def_start_col'],
                        'def_end_col': record['def_end_col']
                    }

            def get_code_snippet(snippet_file_path, start_line, end_line, start_col, end_col):
                with open(snippet_file_path, 'r', encoding='utf-8') as f:
                    code_lines = f.readlines()
                num_lines = end_line - start_line + 1
                if num_lines > 2:
                    end_line = start_line + 1  # Max 2 lines
                    num_lines = 2
                snippet = ''
                for i in range(start_line, end_line + 1):
                    line = code_lines[i].rstrip('\n')
                    if i == start_line:
                        line = line[start_col:]
                    if i == end_line:
                        line = line[:end_col]
                    snippet += line + '\n'
                return snippet.strip()

            refs_details = []
            for ref_id in chunk["refs"]:
                if ref_id in refs_map:
                    data = refs_map[ref_id]
                    ref_text = get_code_snippet(data['ref_file'], data['ref_start_line'], data['ref_end_line'], data['ref_start_col'], data['ref_end_col'])
                    def_text = get_code_snippet(data['def_file'], data['def_start_line'], data['def_end_line'], data['def_start_col'], data['def_end_col'])
                    refs_details.append(f"{ref_text} references {def_text} in {data['def_file']}")
            refs_info = "\n".join(refs_details) if refs_details else ""

            return refs_info

    async def _dispatch_llm_request(self, chunk: Dict, file_path: str, repo_id: str, neo4j_client: Neo4jClient):
        """Build prompt, call LLM for single chunk, store result."""
        # Build prompt: chunk text + refs info (fetch summaries if needed)
        refs_info = await self.get_ref_info(chunk, neo4j_client)
        # print(f"Job {self.job_id}: Processing chunk {chunk['start_line']}:{chunk['end_line']} in {file_path} with refs: {refs_info}")
        prompt = f"\nChunk: {chunk['text']}\n{refs_info}"
        
        # Store
        chunk_id = f"{repo_id}_{file_path}_{chunk['start_line']}_{chunk['end_line']}"

        # only send llm request if chunk_id does not already exist
        existing_doc = self.chunk_summaries_collection.find_one({"chunk_id": chunk_id})
        if existing_doc:
            response = existing_doc.get("summary", "")
        else:
            response = self.llm_client.generate(prompt=prompt, reasoning_effort="low", temperature=0.0)

        doc = {
            "chunk_id": chunk_id,
            "repo_id": repo_id,
            "file_path": file_path,
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "summary": response
        }
        self.chunk_summaries_collection.replace_one({"chunk_id": chunk_id}, doc, upsert=True)
        existing_doc = self.chunk_summaries_collection.find_one({"chunk_id": chunk_id})
        mongo_id = str(existing_doc["_id"])
        # Link in Neo4j (bulk update nodes)

        # print(f"Mapping nodes in the chunk: {chunk['nodes']} to chunk summary ID: {mongo_id}")
        query = """
        UNWIND $node_ids AS node_id
        MATCH (n:ASTNode {node_id: node_id})
        SET n.chunk_summary_id = $mongo_id
        """
        with neo4j_client.driver.session() as session:  # Assuming neo4j_client passed or self.neo4j_client
            session.run(query, node_ids=chunk["nodes"], mongo_id=mongo_id)
        
        # print(f"Job {self.job_id}: Stored chunk {chunk_id} and linked {len(chunk['nodes'])} nodes")