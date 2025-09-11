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
from typing import Dict, List, Any, Tuple, Set
import asyncio
import json
import os
from pathlib import Path
from collections import deque
from tqdm import tqdm

from bson import ObjectId

from .core.base import PipelineStage, StageResult
from ..models.data_model import S3StorageInfo
from ..db import Neo4jClient, get_neo4j_client, get_mongo_client, attention_db_runtime
from ..llm import GroqLLM


class MentalModelStage(PipelineStage):
    """Stage for generating and storing the hierarchical mental model."""
    
    def __init__(self, config: dict = None):
        super().__init__("Mental Model Generation", config)
        self.mongo_client = get_mongo_client()
        self.mental_model_collection = self.mongo_client["mental_model"]
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
            
            insights, ignored_files = await self.generate_repo_overview_2(dir_tree, repo_id)
            print(f"Job {self.job_id}: GENERATED repo overview with {len(insights)} insights, ignoring {len(ignored_files)} files")

            await self._set_potential_entry_points(insights, repo_id)

            # Get _cross_file_interactions_in_file(self, file_path: str, repo_id: str) in insights


            # await self.generate_dependency_graph(repo_id, ignored_files)
            # print(f"Files ignored: {ignored_files}")

            # Step 2: Generate hierarchical summaries bottom-up
            # file_summaries = await self._generate_hierarchical_summary(dir_tree, repo_id, insights)

            # print(f"Job {self.job_id}: GENERATED hierarchical summaries for {len(file_summaries)} files")

            
            # Step 3: Add repo-level summary
            # repo_summary = await self._generate_repo_summary(file_summaries, repo_id)
            # repo_summary = await self.generate_repo_overview(dir_tree, repo_id)
            

            # Step 4: Write to .md file
            # self._write_to_md(repo_summary, file_summaries, repo_id, dir_tree)
            # self._write_to_jsonl(repo_summary, file_summaries, repo_id, dir_tree)

            self._write_to_md(insights, [], repo_id, dir_tree)
            # self._write_to_jsonl(insights, file_summaries, repo_id, dir_tree)
            self._write_to_jsonl_v2(insights, repo_id)

            await attention_db_runtime.build_keybank(repo_id)

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

    async def _set_potential_entry_points(self, insights: List[dict], repo_id: str) -> List[str]:
        """Get potential entry points for the repo based on insights."""
        potential_entry_points = set()
        cross_file_ref_counts = {}

        # Initially add all files to potential_entry_counts
        for insight in insights:
            file_path = insight.get("file_path")
            potential_entry_points.add(file_path)

        for insight in insights:
            # Extract file paths from insights
            file_path = insight.get("file_path")
            cross_file_paths = insight.get("cross_file_paths", {})

            cross_file_ref_counts[file_path] = len(cross_file_paths)

            for cross_file_path in cross_file_paths:
                if cross_file_path in potential_entry_points:
                    potential_entry_points.remove(cross_file_path)

        print(f"Potential entry points: {potential_entry_points}")
        print(f"Cross-file reference counts: {cross_file_ref_counts}")
        if not potential_entry_points:
            if cross_file_ref_counts:
                # Find the minimum number of references
                min_refs = min(cross_file_ref_counts.values())
                # Add files with the minimum number of references to potential entry points
                potential_entry_points.update(
                    file_path for file_path, ref_count in cross_file_ref_counts.items()
                    if ref_count == min_refs
                )
        
        # Add potential entry points to the DB
        for file_path in potential_entry_points:
            document = {
                "repo_id": repo_id,
                "file_path": file_path,
                "document_type": "POTENTIAL_ENTRY_POINTS",
            }
            self.mental_model_collection.update_one(
                {"repo_id": repo_id, "file_path": file_path, "document_type": "POTENTIAL_ENTRY_POINTS"},
                {"$set": document},
                upsert=True
            )

        print(f"Final potential entry points: {potential_entry_points}")
        # Convert set to sorted list for consistent output
        return sorted(list(potential_entry_points))

    async def generate_repo_overview_2(self, dir_tree: Dict, repo_id: str) -> Tuple[Dict[str, str], Set[str]]:
        """Generate a comprehensive overview of the repo by summarizing critical files, ignoring non-critical ones."""

        def get_all_files(node: Dict, path: str = "") -> list[str]:
            files = []
            for file in node.get('files', []):
                fp = str(Path(path) / file) if path else file
                files.append(fp)
            for subdir, subnode in node.get('subdirs', {}).items():
                subpath = str(Path(path) / subdir) if path else subdir
                files.extend(get_all_files(subnode, subpath))
            return files

        async def summarize_file(file_path: str) -> tuple[str, str]:
            # Check mongoDB if we already have file_path in the mental_model_collections BRIEF_FILE_OVERVIEW or IGNORED_FILE document type
            existing_doc = self.mental_model_collection.find_one(
                {
                    "repo_id": repo_id,
                    "file_path": file_path,
                    "document_type": {"$in": ["BRIEF_FILE_OVERVIEW", "IGNORED_FILE"]}
                },
                {"_id": 0, "data": 1}
            )
            if existing_doc:
                return file_path, existing_doc["data"]

            code = None
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            
            # system_prompt = (
            #     "Given the repo_name, file_path and its code - your task is to write a 3-4 line summary of what the code does "
            #     "only if its critical to understanding the behaviour of the code repo otherwise simply output \"IGNORE\" "

            #     "Ignore: "
            #     "- tutorial example files"
            #     "- tests"
            #     "- docs"

            #     "While paying attention to the code, also pay attention to where in the repo the file is located."
            # )

            system_prompt = "Given the repo_name, file_path and its code - your task is to write a 3-4 line summary of what the code does only if its critical to understanding the behaviour of the code repo otherwise simply output \\\"IGNORE\\\". \n\nIgnore:\n- tutorial example files, \n- tests \n- docs. \n\nWhile paying attention to the code, also pay attention to where in the repo the file is located."

            prompt = f"Repo: {repo_id}\n\nFile: {file_path}\n\nCode:\n\n{code}"
            response = await self.llm_client.generate_async(
                prompt=prompt,
                system_prompt=system_prompt,
                reasoning_effort="medium",
                temperature=0.0
            )
            return file_path, response.strip()

        all_files = get_all_files(dir_tree)

        #Make insights like {"file_path": <file_path>, "summary": <summary>}
        insights = []
        ignored: Set[str] = set()
        
        batch_size = 20
        pbar = tqdm(total=len(all_files), desc="Processing files")
        
        for i in range(0, len(all_files), batch_size):
            batch = all_files[i:i + batch_size]
            tasks = [summarize_file(fp) for fp in batch]
            results = await asyncio.gather(*tasks)
            
            for fp, summary in results:
                # if summary.startswith("IGNORE"):
                if summary == "IGNORE":
                    ignored.add(fp)
                else:
                    insights.append({"file_path": fp, "summary": summary})

            pbar.update(len(batch))
        
        pbar.close()

        # Add insights to the mental model mongodb collection
        for insight in insights:
            document = {
                "repo_id": repo_id,
                "file_path": insight["file_path"],
                "document_type": "BRIEF_FILE_OVERVIEW",
                "data": insight["summary"]
            }
            self.mental_model_collection.update_one(
                {"repo_id": repo_id, "file_path": insight["file_path"], "document_type": "BRIEF_FILE_OVERVIEW"},
                {"$set": document},
                upsert=True
            )

        # Add ignored files to the mental model mongodb collection
        for file_path in ignored:
            document = {
                "repo_id": repo_id,
                "file_path": file_path,
                "document_type": "IGNORED_FILE",
                "data": "IGNORE"
            }
            self.mental_model_collection.update_one(
                {"repo_id": repo_id, "file_path": file_path, "document_type": "IGNORED_FILE"},
                {"$set": document},
                upsert=True
            )
        
        # Add _cross_file_interactions_in_file in insights
        for insight in insights:
            file_path = insight["file_path"]
            interactions, cross_file_paths = self._cross_file_interactions_in_file(file_path, repo_id)
            insight["cross_file_interactions"] = interactions
            insight["cross_file_paths"] = cross_file_paths

        return insights, ignored

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

    async def _generate_hierarchical_summary(self, dir_tree: Dict, repo_id: str, insights: Dict[str, str]) -> Dict:
        """Generate detailed summaries bottom-up for files in the insights dictionary, using insights to focus on novel information."""
        from tqdm import tqdm
        
        async def summarize_file(file_path: str, insight_summary: str) -> str:
            code = None
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            
            system_prompt = (
                "You are an analytical code summarizer tasked with processing code of a single file. "
                "Your goal is to deduce what the file does, how it does it, and how its components interact, "
                "focusing on novel or distinctive information not covered in the provided high-level summary. "
                "Preserve critical elements like classes, methods, and variables, emphasizing their unique roles and traits. "
                "Retain essential details, avoiding redundancy with the high-level summary. "
                "Output in the exact format below, ensuring concise, relevant, dense content:\n\n"
                "Overview: 1-2 sentences summarizing the file's unique or non-obvious functionality, beyond the high-level summary.\n"
                "Key Components:\n- Bullet: Class/Method/Variable X - its role and a distinctive or unexpected trait.\n"
                "...\n"
                "Interactions:\n- Bullet: A specific flow or pattern (e.g., Method A triggers Variable B update or Class C delegates to Method D).\n"
                "...\n"
                "Do not include code snippets or mention the system prompt in the output. "
                "Provide only the requested output, strictly adhering to the specified format."
            )

            interactions, _ = self._cross_file_interactions_in_file(file_path, repo_id)
            cross_refs = "\n".join(interactions) if interactions else "No cross-file dependencies."

            prompt = (
                f"{file_path} in repo {repo_id}:\n\n{code}\n\n"
                f"Cross-file interactions: {cross_refs}\n\n"
                f"High-level summary from prior analysis: {insight_summary or 'No prior summary available.'}"
            )
            response = await self.llm_client.generate_async(
                prompt=prompt,
                system_prompt=system_prompt,
                reasoning_effort="medium",
                temperature=0.0,
            )
            return response.strip()

        # Process only files present in insights
        file_summaries = []
        file_paths = list(insights.keys())
        batch_size = 20

        # Initialize progress bar
        pbar = tqdm(total=len(file_paths), desc="Summarizing files")

        # Verify and collect valid files
        valid_file_paths = []
        for file_path in file_paths:
            path_parts = Path(file_path).parts
            current_node = dir_tree
            file_exists = False
            for part in path_parts[:-1]:
                current_node = current_node.get('subdirs', {}).get(part, {})
            if path_parts[-1] in current_node.get('files', []):
                file_exists = True
                valid_file_paths.append(file_path)
            else:
                print(f"Job {self.job_id}: File {file_path} from insights not found in dir_tree; skipping.")
        
        # Process files in batches of 20
        for i in range(0, len(valid_file_paths), batch_size):
            batch = valid_file_paths[i:i + batch_size]
            file_tasks = [
                asyncio.create_task(summarize_file(fp, insights.get(fp, "")))
                for fp in batch
            ]
            
            # Gather file summaries for the batch
            file_results = await asyncio.gather(*file_tasks, return_exceptions=True)
            
            for file_path, result in zip(batch, file_results):
                if isinstance(result, str):  # Ensure no exceptions
                    interactions, _ = self._cross_file_interactions_in_file(file_path, repo_id)
                    file_info = {
                        "path": file_path,
                        "summary": result or "No summary available.",
                        "cross_file_interactions": interactions,
                    }
                    file_summaries.append(file_info)
                else:
                    print(f"Job {self.job_id}: Failed to summarize {file_path}: {str(result)}")
            
            pbar.update(len(batch))
        
        pbar.close()

        # Add file_summaries to the MongoDB mental model collection
        for file_info in file_summaries:
            document = {
                "repo_id": repo_id,
                "file_path": file_info["path"],
                "document_type": "FILE_SUMMARY",
                "data": file_info
            }
            self.mental_model_collection.update_one(
                {"repo_id": repo_id, "file_path": file_info["path"], "document_type": "FILE_SUMMARY"},
                {"$set": document},
                upsert=True
            )

        return {'files': file_summaries}

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
            # Collect records into a list to avoid exhausting the result cursor
            records = list(result)
            
            interactions = [
                f"{record['ref_name']} REFERENCES {record['node_type']} IN {record['def_file_path']}"
                for record in records
            ]

            # Add each record['def_file_path'] in a set to get rid of duplicates
            cross_file_paths = {record['def_file_path'] for record in records}

            return interactions, cross_file_paths

    def _write_to_md(self, repo_summary: List[Dict], file_summaries: Dict, repo_id: str, dir_tree: Dict) -> None:
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

        repo_summary_md = []
        file_number = 1
        repo_summary_md.append(f"# Mental Model for Repository: {repo_id}\n")
        repo_summary_md.append("This document outlines the critical files and their interactions to understand how the repository functions.\n")
        repo_summary_md.append("## Critical Files\n")

        for insight in repo_summary:
            interaction_list = "\n".join([f"- {interaction}" for interaction in insight['cross_file_interactions']])
            repo_summary_md.append(f"### {file_number}. `{insight['file_path']}`\n")
            repo_summary_md.append(f"**Summary**: {insight['summary']}\n")
            repo_summary_md.append("**Cross-File Interactions**:\n" + (interaction_list if interaction_list else "- None\n") + "\n")
            file_number += 1

        repo_summary_md_str = "\n".join(repo_summary_md)

        # Start Markdown content
        markdown_content = [
            repo_summary_md_str,
            "---",
        ]

        # Write to file
        if output_path.exists():
            print(f"Job {self.job_id}: Overwriting existing mental model file: {output_path}")

        with output_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(markdown_content))
        
        print(f"Job {self.job_id}: Wrote mental model to {output_path}")

    def _write_to_jsonl(self, repo_summary: dict, file_summaries: Dict, repo_id: str, dir_tree: Dict) -> None:
        """
        Emit attentionDB-compatible JSONL records for:
        - repo (1 record)
        - directories (one per dir)
        - files (one per file)
        Fields align with the Record schema used by attentiondb.build-keys.

        Expected inputs:
        - repo_summary: str
        - file_summaries: {"files":[{"path": "...", "summary": "...", "cross_file_interactions":[...], ...}, ...]}
        - dir_tree: {"files":[...], "subdirs": { "subdir": {...}, ... } }
        """
        output_dir = Path(f"{repo_id}/mental_model")
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "mental_model.jsonl"

        def write_jsonl(records: List[Dict[str, Any]], path: Path):
            with path.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

        def norm_path(p: str) -> str:
            return p.replace("\\", "/")

        def dir_records_from_tree(root_id: str, tree: Dict, prefix: str = "") -> List[Dict[str, Any]]:
            """Collect all file paths from dir_tree into a single record."""
            file_paths: List[str] = []

            def collect_files(node: Dict, current_prefix: str = "") -> None:
                """Helper function to recursively collect file paths."""
                dir_path = norm_path(current_prefix)
                # Collect files in current directory
                for file_name in sorted(node.get("files", [])):
                    file_path = f"{dir_path}/{file_name}" if dir_path else file_name
                    file_paths.append(file_path)
                # Recurse into subdirectories
                for sub, subnode in sorted(node.get("subdirs", {}).items()):
                    sub_prefix = f"{dir_path}/{sub}" if dir_path else sub
                    collect_files(subnode, sub_prefix)

            # Collect all file paths
            collect_files(tree, prefix)

            # Return a single record with all file paths
            return [{
                "id": root_id,
                "type": "file_paths_in_repo",
                "file_paths": file_paths
            }]
        
        repo_summary_md = []
        file_number = 1
        for file_path, summary in repo_summary.items():
            repo_summary_md.append(f"{file_number}. File: {file_path}\n{summary}\n\n")
            file_number += 1

        repo_summary_md_str = "\n".join(repo_summary_md)

        # 1) Repo record
        repo_rec = {
            "id": f"repo::{repo_id}",
            "type": "repo",
            "summary": (repo_summary_md_str or "").strip(),
        }

        # 2) Directory records
        dir_records = dir_records_from_tree(repo_id, dir_tree)

        # 3) File records
        file_records: List[Dict[str, Any]] = []
        for fi in file_summaries.get("files", []):
            path = norm_path(fi.get("path", ""))
            if not path:
                continue
            file_id = f"file::{path}"
            name = os.path.basename(path)

            # Map cross_file_interactions → deps (best-effort; keep as strings if unknown)
            # If your strings have structure (e.g., "calls: file::core/db.py:connect"), you can parse more precisely.
            interactions: List[str] = fi.get("cross_file_interactions", []) or []
            deps_imports, deps_calls = [], []
            for s in interactions:
                s_norm = str(s).strip()
                deps_imports.append(s_norm)

            file_records.append({
                "id": file_id,
                "type": "file",
                "path": path,
                "name": name,
                "summary": (fi.get("summary") or "").strip(),
                "deps": {
                    "imports": deps_imports,
                },
                # "subsystem": subsystem,
                # "spans": fi.get("spans", []),                   # if you tracked file:line evidence, add it here
                # "size_loc": fi.get("size_loc"),                 # optional
                # "centrality": fi.get("centrality"),             # optional precomputed importance
                # "commit_sha": fi.get("commit_sha"),             # optional
            })

        # 4) Write JSONL (repo → dirs → files)
        records_all = [repo_rec] + dir_records + file_records
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(records_all, out_path)
        print(f"Job {self.job_id}: Wrote attentionDB JSONL ({len(records_all)} records) to {out_path}")

    def _write_to_jsonl_v2(self, insights: List[Dict[str, Any]], repo_id: str) -> None:
        """
        Write one JSONL record per insight.

        Expected inputs:
        - insights: [
            {
            "file_path": "<file_path>",
            "summary": "<summary>",
            "cross_file_paths": ["<file_path>", ...]
            },
            ...
        ]
        - repo_id: string

        Output (mental_model/mental_model.jsonl):
        Each line is:
        {
        "id": "<file_path>",
        "file_path": "<file_path>",
        "summary": "<summary>",
        "cross_file_deps": "<stringified cross_file_paths paths>"
        }
        """

        def norm_path(p: str) -> str:
            return (p or "").replace("\\", "/")

        output_dir = Path(f"{repo_id}/mental_model")
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "mental_model.jsonl"

        records: List[Dict[str, Any]] = []

        for item in insights or []:
            raw_path = item.get("file_path", "")
            path = norm_path(raw_path)
            if not path:
                # Skip malformed entries with no path
                continue

            summary = (item.get("summary") or "").strip()

            cross = item.get("cross_file_paths") or []
            # Ensure list-of-strings, normalize, then stringify as a single comma-separated string
            cross_norm = [norm_path(str(p)) for p in cross if p]
            cross_str = ", ".join(cross_norm)

            records.append({
                "id": path,
                "file_path": path,
                "summary": summary,
                "cross_file_deps": cross_str,
            })

        with out_path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        print(f"Job {self.job_id}: Wrote {len(records)} JSONL records to {out_path}")
    
    def validate_config(self) -> bool:
        return True