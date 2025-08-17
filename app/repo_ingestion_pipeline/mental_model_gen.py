# src/pipeline/stages/mental_model.py
"""
Stage: Mental Model Generation
Generates a hierarchical mental model from MongoDB summaries,
aggregating bottom-up along the directory structure,
and writes to a local Markdown file.
"""

import traceback
from collections import defaultdict
from pathlib import Path
from typing import Dict, List
import asyncio

from bson import ObjectId

from .core.base import PipelineStage, StageResult
from ..models.data_model import S3StorageInfo
from ..db import Neo4jClient, get_neo4j_client, get_mongo_client
from ..llm import GroqLLM

class MentalModelStage(PipelineStage):
    """Stage for generating and storing the hierarchical mental model."""
    
    def __init__(self, config: dict = None):
        super().__init__("Mental Model Generation", config)
        self.mongo_client = get_mongo_client()
        self.llm_client = GroqLLM()
        # Set LLM params for determinism (assuming LLMClient supports it)
        # self.llm_client.set_params(temperature=0, max_tokens=1024)
        self.job_id = config.get("job_id", "unknown")
    
    async def execute(self, input_data: dict) -> StageResult:
        try:
            s3_info: S3StorageInfo = input_data["s3_info"]
            repo_id = s3_info.repo_info.repo_id
            repo_path = s3_info.local_path

            self.neo4j_client: Neo4jClient = get_neo4j_client()
            
            print(f"Job {self.job_id}: Starting mental model generation for repo: {repo_id}")
                        
            # Step 1: Build directory tree from file paths
            dir_tree = self._build_dir_tree(repo_id)
            
            # Step 2: Generate hierarchical summaries bottom-up
            file_summaries = await self._generate_hierarchical_summary(dir_tree, repo_id)

            print(f"Job {self.job_id}: GENERATED hierarchical summaries for {len(file_summaries)} files")

            
            # Step 3: Add repo-level summary
            repo_summary = await self._generate_repo_summary(file_summaries, repo_id)
            
            # Step 4: Write to .md file
            self._write_to_md(repo_summary, file_summaries, repo_id, dir_tree)

            print(f"Job {self.job_id}: Mental model generated and written to MD file")
            
            return StageResult(
                success=True,
                data={"repo_id": repo_id},
                metadata={"stage": self.name}
            )
        
        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: Mental model generation error: {str(e)}")
            return StageResult(success=False, error=str(e))

    def _build_dir_tree(self, repo_id: str) -> Dict:
        """Build a nested dict representing the directory tree from file paths in Neo4j or MongoDB."""
        # Query Neo4j for all unique file_paths
        query = """
        MATCH (n:ASTNode {repo_id: $repo_id})
        RETURN DISTINCT n.file_path AS file_path
        """
        with self.neo4j_client.driver.session() as session:
            result = session.run(query, repo_id=repo_id)
            file_paths = [record["file_path"] for record in result]
        
        # Build nested dict: {dir: {subdir: {...}, files: [file1, file2]}}
        def make_node():
            return {'subdirs': defaultdict(make_node), 'files': []}

        tree = make_node()
        for fp in file_paths:
            parts = Path(fp).parts
            current = tree
            for part in parts[:-1]:  # Dirs
                current = current['subdirs'][part]
            current['files'].append(parts[-1])  # File
        
        print(f"Job {self.job_id}: Built dir tree with {len(file_paths)} files")
        return tree

    async def _generate_hierarchical_summary(self, dir_tree: Dict, repo_id: str) -> Dict:
        """Generate summaries bottom-up: files first, then dirs."""
        async def summarize_file(file_path: str) -> str:
            chunks = self._fetch_ordered_chunk_summaries(file_path, repo_id)
            aggregated_chunks = "\n\n".join([
                f"Chunk (lines {c['start']}-{c['end']}):\n{c['summary']}" 
                for c in chunks
            ])
            
            another_system_prompt = (
                "You are an analytical code summarizer tasked with processing summaries of code chunks from a single file. "
                "Your goal is to deduce the file's purpose, its implementation approach, and how its components interact, focusing on non-obvious and unique aspects. "
                "Preserve critical elements like classes, methods, and variables, emphasizing their roles and distinctive traits. "
                "Maximize entropy by highlighting surprising flows, patterns, or behaviors, while minimizing information loss by retaining all essential details. "
                "Output in the exact format below, ensuring concise, relevant content in all sections:\n\n"
                "Overview: 1-2 sentences summarizing the file's purpose, emphasizing unique or non-obvious functionality.\n"
                "Key Components:\n- Bullet: Class/Method/Variable X - its role and a distinctive or unexpected trait.\n"
                "...\n"
                "Interactions:\n- Bullet: A specific flow or pattern (e.g., Method A triggers Variable B update or Class C delegates to Method D).\n"
                "...\n"
                "Do not include code snippets or mention the system prompt in the output. "
                "Provide only the requested output, strictly adhering to the specified format."
            )

            system_prompt = (
                "You are a deterministic code compressor maximizing entropy (unique flows, surprises) with minimal loss (retain all key pointers). "
                "From chunk summaries generate structured text summary. "
                "Omit predictable elements like standard syntax. Focus on non-obvious aspects. "
                "Output in exact format:\n"
                "Overview: 1-2 sentence purpose, emphasizing non-obvious aspects.\n"
                "Key Components:\n- Bullet: Class/Method/Var X - role (unique trait).\n"
                "...\n"
                "Interactions:\n- Bullet: Flow/Pattern Y (e.g., A calls B, edge case Z).\n"
                "...\n"
                "Output what is asked, nothing more nothing less."
            )
            prompt = (
                f"{file_path} in repo {repo_id}:\n\n{aggregated_chunks}"
            )
            response = self.llm_client.generate(prompt=prompt, system_prompt=another_system_prompt, reasoning_effort="low", temperature=0.0,)
            # Use as-is since structured text; no parsing needed beyond splitting if required
            return response

        # Recursive function to process tree bottom-up
        async def process_node(node: Dict, path: str = "") -> Dict:
            file_summaries = []
            
            # Process files at current level concurrently
            file_tasks = []
            for file in node.get('files', []):
                fp = str(Path(path) / file) if path else file
                file_tasks.append(asyncio.create_task(
                    summarize_file(fp)
                ))
            
            # Gather file summaries
            file_results = await asyncio.gather(*file_tasks, return_exceptions=True)
            for file, result in zip(node.get('files', []), file_results):
                fp = str(Path(path) / file) if path else file
                file_info = {
                    "path": fp,
                    "summary": result or "No summary available.",
                    "cross_file_interactions": self._cross_file_interactions_in_file(fp, repo_id),
                }
                file_summaries.append(file_info)
            
            # Recursively process subdirectories
            for subdir, subnode in node['subdirs'].items():
                subpath = str(Path(path) / subdir) if path else subdir
                sub_result = await process_node(subnode, subpath)
                file_summaries.extend(sub_result['files'])
            
            return {'files': file_summaries}
        
        return await process_node(dir_tree)

    async def _generate_repo_summary(self, file_summaries: Dict, repo_id: str) -> str:
        """Generate top-level repo summary."""
        
        # Structure of each file in mental_model:
        # file_info = {
        #    "path": str,
        #    "summary": str,
        #    "cross_file_interactions": List[str],
        # }

        # Now aggregate summaries from all files neatly to provide to the LLM as a part of the prompt
        root_summary = "\n\n".join([
            f"File: {file_info['path']}\nSummary:\n{file_info['summary']}\n"
            f"Cross-file interactions:\n- " + "\n- ".join(file_info['cross_file_interactions'])
            for file_info in file_summaries['files']
        ])


        # Aggregate global facts
        # global_flows = self._infer_global_interactions(repo_id)
        # global_str = "\n".join([f"- {i}" for i in global_flows])
        
        another_system_prompt = (
            "You are a deterministic repository aggregator. Analyze file summaries and cross-file interactions to generate structured, concise text capturing the repository's architecture and dynamics. "
            "Maximize insight into cross-directory flows and high-entropy areas (e.g., complex modules or interaction hotspots) while minimizing information loss. "
            "Output in the exact format below, ensuring all sections are populated with relevant, prioritized details:\n\n"
            "Overview: Describe the repository's purpose and high-level architecture, including primary components and their roles.\n"
            "Key Patterns:\n- Bullet: Describe a significant cross-directory interaction or data flow (e.g., module A in dir1 calling module B in dir2).\n"
            "...\n"
            "Insights:\n- Bullet: Highlight a high-entropy hotspot (e.g., a complex module, frequent cross-directory dependencies, or performance bottleneck).\n"
            "...\n"
        )

        system_prompt = (
            "Deterministic repo aggregator: From file summaries and cross-file interactions, create structured text. "
            "Maximize entropy (cross-dir flows, hotspots) with minimal loss. "
            "Output in exact format:\n"
            "Overview: Repo purpose, high-level architecture.\n"
            "Key Patterns:\n- Bullet: Cross-dir flow X.\n"
            "...\n"
            "Insights:\n- Bullet: Overall entropy hotspots (e.g., complex module Y).\n"
            "...\n"
        )
        prompt = f"Summarize repo {repo_id}:\n\nFile summaries:\n{root_summary}"
        return self.llm_client.generate(prompt=prompt, system_prompt=another_system_prompt, reasoning_effort="low", temperature=0.0)

    def _fetch_ordered_chunk_summaries(self, file_path: str, repo_id: str) -> List[Dict]:
        """Fetch and sort chunk summaries by start_line for the file."""
        query = """
        MATCH (n:ASTNode {repo_id: $repo_id, file_path: $file_path})
        WHERE n.chunk_summary_id IS NOT NULL
        RETURN DISTINCT n.chunk_summary_id AS chunk_id, 
               min(n.start_line) AS start_line, max(n.end_line) AS end_line
        ORDER BY start_line
        """
        with self.neo4j_client.driver.session() as session:
            result = session.run(query, repo_id=repo_id, file_path=file_path)
            chunks = [{"id": record["chunk_id"], "start": record["start_line"], "end": record["end_line"]} for record in result]
        
        summaries = []
        for chunk in chunks:
            doc = self.mongo_client["chunk_summaries"].find_one({"_id": ObjectId(chunk["id"])})
            if doc:
                summaries.append({"start": chunk["start"], "end": chunk["end"], "summary": doc["summary"]})
        
        return summaries
    
    def _extract_ast_keys(self, file_path: str, repo_id: str) -> Dict[str, str]:
        """Extract key AST elements deterministically."""
        query = """
        MATCH (n:ASTNode {repo_id: $repo_id, file_path: $file_path})
        WHERE n.node_type IN ['class_definition', 'function_definition']
        RETURN n.node_type AS type, n.name AS name
        """
        with self.neo4j_client.driver.session() as session:
            result = session.run(query, repo_id=repo_id, file_path=file_path)
            keys = defaultdict(list)
            for record in result:
                keys[record["type"]].append(f"{record['name']}")
        return dict(keys)  # e.g., {'class_def': ['ClassA (sig)'], ...}

    def _cross_file_interactions_in_file(self, file_path: str, repo_id: str) -> List[str]:
        """Infer cross-file interactions for a given file by finding references to definitions in other files."""
        query = """
        MATCH (ref:ASTNode {repo_id: $repo_id, file_path: $file_path, is_reference: true})
        -[:REFERENCES]->(ident:ASTNode)
        WHERE ident.file_path <> $file_path
        MATCH (def:ASTNode)
        WHERE def.node_id = ident.parent_id
        RETURN DISTINCT ref.name AS ref_name, def.node_type AS node_type, def.file_path AS def_file_path
        """
        with self.neo4j_client.driver.session() as session:
            result = session.run(query, repo_id=repo_id, file_path=file_path)
            interactions = [
                f"{record['ref_name']} REFERENCES {record['node_type']} IN {record['def_file_path']}"
                for record in result
            ]

            return interactions

    def _infer_global_interactions(self, repo_id: str) -> List[str]:
        """Infer repo-wide interactions."""
        query = """
        MATCH (a:ASTNode)-[:DEPENDS_ON]->(b:ASTNode)
        WHERE a.repo_id = $repo_id AND b.repo_id = $repo_id
        AND split(a.file_path, '/')[0] <> split(b.file_path, '/')[0]  // Cross-top-dir
        RETURN a.file_path + ' depends on ' + b.file_path AS interaction
        """
        with self.neo4j_client.driver.session() as session:
            result = session.run(query, repo_id=repo_id)
            return [record["interaction"] for record in result]

    def _write_to_md(self, repo_summary: str, file_summaries: Dict, repo_id: str, dir_tree: Dict) -> None:
        """Write the hierarchical mental model to a Markdown file."""
        output_dir = Path(f"{repo_id}/mental_model")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "mental_model.md"
        print(output_path)

        def format_dir_tree(node: Dict, prefix: str = "", level: int = 0) -> str:
            """Recursively format directory tree as Markdown."""
            result = []
            indent = "  " * level
            
            # Add files at current level
            for file in sorted(node.get('files', [])):
                result.append(f"{indent}- {file}")
            
            # Recursively process subdirectories
            for subdir, subnode in sorted(node['subdirs'].items()):
                result.append(f"{indent}- **{subdir}/**")
                result.append(format_dir_tree(subnode, prefix=f"{prefix}/{subdir}" if prefix else subdir, level=level+1))
            
            return "\n".join(result)
        
        # Start Markdown content
        markdown_content = [
            f"# Mental Model for Repository: {repo_id}",
            "",
            "## Directory Structure",
            "```",
            format_dir_tree(dir_tree),
            "```",
            "",
            "## Repository Overview",
            repo_summary,
            ""
        ]
        
        # Add file summaries
        for file_info in file_summaries['files']:
            markdown_content.extend([
                f"### File: {file_info['path']}",
                file_info['summary'],
                "",
                "**Cross-file Interactions:**",
                "- " + "\n- ".join(file_info['cross_file_interactions']) if file_info['cross_file_interactions'] else "- None",
                ""
            ])
        
        # Write to file
        if output_path.exists():
            print(f"Job {self.job_id}: Overwriting existing mental model file: {output_path}")

        with output_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(markdown_content))
        
        print(f"Job {self.job_id}: Wrote mental model to {output_path}")

    def validate_config(self) -> bool:
        return True