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

import asyncio
from pathlib import Path
from typing import Dict, List, Tuple, Set, Any
import re
import ast


from bson import ObjectId

from ..db import Neo4jClient, get_neo4j_client, get_mongo_client
from ..llm import GroqLLM
# from ..repo_arch_service import build_repo_architecture_v2
from ..repo_arch_service_me import build_repo_architecture_v2
from ..reverse_eng_service import reverse_engineer


# --- add this helper anywhere in the class/file (module-level is fine) ---
def _safe_json_loads(text: str, default_obj=None):
    """
    Attempt to parse sloppy LLM JSON robustly.
    - Strips code fences and prose around JSON
    - Normalizes smart quotes
    - Removes JS-style comments
    - Removes trailing commas
    - Extracts the largest balanced {...} block
    - Falls back to ast.literal_eval for single-quoted dicts
    Returns default_obj (or {}) on failure.
    """
    if default_obj is None:
        default_obj = {}

    if not isinstance(text, str):
        return default_obj

    s = text.strip()

    # 1) Strip code fences ```json ... ``` or ``` ... ```
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)

    # 2) Normalize smart quotes
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u201e", '"').replace("\u201f", '"')
    s = s.replace("\u2018", "'").replace("\u2019", "'").replace("\u2032", "'").replace("\u2033", '"')

    # 3) Remove JS-style comments
    s = re.sub(r"//.*?$", "", s, flags=re.MULTILINE)                  # // line comments
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)                  # /* block comments */

    # 4) Extract the largest balanced JSON object via brace stack
    def extract_largest_braced_block(txt: str):
        starts = []
        best = None
        for i, ch in enumerate(txt):
            if ch == "{":
                starts.append(i)
            elif ch == "}":
                if starts:
                    start = starts.pop()
                    cand = txt[start:i+1]
                    if (best is None) or (len(cand) > len(best)):
                        best = cand
        return best

    candidate = extract_largest_braced_block(s) or s

    # 5) Remove trailing commas before } or ]
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)

    # 6) Try strict json
    try:
        return json.loads(candidate)
    except Exception:
        pass

    # 7) Try literal_eval (handles single quotes)
    try:
        return ast.literal_eval(candidate)
    except Exception:
        return default_obj

class MentalModelStage():
    """Stage for generating and storing the hierarchical mental model."""
    
    def __init__(self, config: dict = None):
        self.mongo_client = get_mongo_client()
        self.mental_model_collection = self.mongo_client["mental_model"]
        self.llm_client = GroqLLM()
        # Set LLM params for determinism (assuming LLMClient supports it)
        # self.llm_client.set_params(temperature=0, max_tokens=1024)
        self.job_id = config.get("job_id", "unknown")
    
    async def run(self, repo_id: str, local_repo_path: Path):
        try:
            repo_path = local_repo_path

            self.neo4j_client: Neo4jClient = get_neo4j_client()
            
            print(f"Job {self.job_id}: Starting mental model generation for repo: {repo_id}")
                        
            # Step 1: Build directory tree from file paths
            dir_tree = self._build_dir_tree(repo_id)
            
            insights, ignored_files = await self.identify_critical_files(dir_tree, repo_id)
            print(f"Job {self.job_id}: GENERATED repo overview with {len(insights)} insights, ignoring {len(ignored_files)} files")
            # repo_summary = await self.generate_repo_summary(insights, repo_id)
            # await self.find_entry_points(repo_id)
            await self._set_potential_entry_points(insights, repo_id)
            # await build_repo_architecture_v2(repo_id)
            await reverse_engineer(repo_id)
        
        except Exception as e:
            traceback.print_exc()
            print(f"Job {self.job_id}: Mental model generation error: {str(e)}")

    async def find_entry_points(self, repo_id: str) -> list:
        """Identify entry points in the repository from existing BRIEF_FILE_OVERVIEW documents.
        Returns a validated list of entry point file paths.
        Minimizes information loss by processing all files together when possible.
        """
        import asyncio
        import json
        from datetime import datetime, timezone

        # 1 token ≈ 4 chars. Conservative budget for context + response overhead
        MAX_CHARS_PER_CALL = 70000
        PROMPT_OVERHEAD = 2000

        # Check MongoDB for existing entry points
        existing = self.mental_model_collection.find_one(
            {"repo_id": repo_id, "document_type": "REPO_ENTRY_POINTS"}
        )
        if existing:
            print(f"Job {self.job_id}: REPO_ENTRY_POINTS already exists for repo {repo_id}, skipping.")
            return existing.get("entry_points", [])

        # Fetch all file overviews
        insights_cursor = self.mental_model_collection.find(
            {"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW"},
            {"_id": 0, "file_path": 1, "data": 1}
        )
        insights = list(insights_cursor)
        if not insights:
            print(f"Job {getattr(self, 'job_id', 'NA')}: No BRIEF_FILE_OVERVIEW documents found for repo {repo_id}")
            return []

        pairs = [{"file_path": d["file_path"], "summary": (d["data"] or "").strip()} for d in insights]
        allowed_paths = [p["file_path"] for p in pairs]

        def create_batches(pairs):
            """Create batches that fit within MAX_CHARS_PER_CALL budget."""
            batches = []
            current_batch = []
            current_size = 0
            limit = MAX_CHARS_PER_CALL - PROMPT_OVERHEAD
            
            for p in pairs:
                item_txt = f"FILE: {p['file_path']}\nSUMMARY: {p['summary']}\n---\n"
                item_size = len(item_txt)
                
                # If adding this item exceeds limit, start new batch
                if current_batch and current_size + item_size > limit:
                    batches.append(current_batch)
                    current_batch = []
                    current_size = 0
                
                current_batch.append(p)
                current_size += item_size
            
            # Add the last batch
            if current_batch:
                batches.append(current_batch)
            
            return batches

        async def find_entry_points_in_batch(batch_pairs):
            """Ask LLM to identify entry points from a batch of files."""
            # Serialize batch
            serialized = "\n".join(
                f"FILE: {p['file_path']}\nSUMMARY: {p['summary']}\n---"
                for p in batch_pairs
            )
            file_paths_list = "\n".join(p["file_path"] for p in batch_pairs)
            
            system_prompt = (
                f"Repository: {repo_id}\n\n"
                "You are analyzing a codebase to identify entry points - files that serve as starting points "
                "for running or using the application. Based on the file paths and their summaries, determine "
                "which files are entry points.\n\n"
                "Return ONLY valid JSON: {\"entry_points\": [\"path1\", \"path2\", ...]}\n"
                "Only include paths from the provided file list."
            )
            
            user_prompt = (
                "FILES AND SUMMARIES:\n"
                "<<<\n" + serialized + "\n>>>\n\n"
                "VALID FILE PATHS (choose only from these):\n" + file_paths_list + "\n\n"
                "Identify which files are entry points. Return JSON with key 'entry_points' containing a list of file paths."
            )
            
            out = await self.llm_client.generate_async(
                prompt=user_prompt,
                system_prompt=system_prompt,
                reasoning_effort="high",
                temperature=0.0,
            )
            
            text = (out or "").strip()
            obj = _safe_json_loads(text, default_obj={"entry_points": []})
            
            # Validate paths against this batch
            valid_paths = set(p["file_path"] for p in batch_pairs)
            entry_points = [ep for ep in (obj.get("entry_points") or []) if ep in valid_paths]
            
            return entry_points

        # Create batches that fit budget
        batches = create_batches(pairs)
        print(f"Job {self.job_id}: Processing {len(pairs)} files in {len(batches)} batch(es)")
        
        # Process each batch sequentially
        all_entry_points = []
        for i, batch in enumerate(batches, 1):
            print(f"Job {self.job_id}: Processing batch {i}/{len(batches)} ({len(batch)} files)")
            batch_entry_points = await find_entry_points_in_batch(batch)
            all_entry_points.extend(batch_entry_points)
        
        # Deduplicate preserving order
        seen = set()
        entry_points = [ep for ep in all_entry_points if not (ep in seen or seen.add(ep))]

        # Persist to MongoDB
        document = {
            "repo_id": repo_id,
            "document_type": "REPO_ENTRY_POINTS",
            "entry_points": entry_points,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "method": "focused_entry_point_detection_v2"
        }
        self.mental_model_collection.update_one(
            {"repo_id": repo_id, "document_type": "REPO_ENTRY_POINTS"},
            {"$set": document},
            upsert=True
        )

        return entry_points

    async def _set_potential_entry_points(self, insights: List[dict], repo_id: str) -> List[str]:
        """Get potential entry points for the repo based on insights."""
        potential_entry_points = set()
        insights_files = {i.get("file_path") for i in insights if i.get("file_path")}
        
        for i in insights:
            fp = i.get("file_path")
            if fp:
                potential_entry_points.add(fp)

        for i in insights:
            fp = i.get("file_path")
            if fp == "data/xai-sdk-python/src/xai_sdk/sync/__init__.py":
                print(f"Checking file: {fp} with upstream deps: {i.get('upstream_dep_files')}")

            if not fp:
                continue
            ups = i.get("upstream_dep_files") or []
            for u in ups:
                if u in insights_files:
                    potential_entry_points.discard(fp)
                    break

        print(f"Potential entry points after upstream dependency check: {potential_entry_points}")

        
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

        # print(f"Final potential entry points: {potential_entry_points}")
        # Convert set to sorted list for consistent output
        return sorted(list(potential_entry_points))

    async def identify_critical_files(self, dir_tree: Dict, repo_id: str) -> Tuple[Dict[str, str], Set[str]]:
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

            system_prompt = (
                "Your task is to analyze the provided code file and determine if it is critical to the core functionality of the repository. "
                "If the file is identified as critical, explain 2-3 lines why it is important, focusing on its role and impact within the codebase. "
                "Simply output \"IGNORE\" if the file is not critical.\n\n"
                "Ignore:\n"
                "- tutorial example files,\n"
                "- tests,\n"
                "- docs.\n\n"
                "While analyzing the code, also consider where in the repository the file is located."
            )

            prompt = f"Repo: {repo_id}\n\nFile: {file_path}\n\nCode:\n\n{code}\n\n Output 'IGNORE' if the file is not critical."
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
                    document = {
                        "repo_id": repo_id,
                        "file_path": fp,
                        "document_type": "IGNORED_FILE",
                        "data": "IGNORE"
                    }
                    self.mental_model_collection.update_one(
                        {"repo_id": repo_id, "file_path": fp, "document_type": "IGNORED_FILE"},
                        {"$set": document},
                        upsert=True
                    )
                else:
                    insights.append({"file_path": fp, "summary": summary})
                    document = {
                        "repo_id": repo_id,
                        "file_path": fp,
                        "document_type": "BRIEF_FILE_OVERVIEW",
                        "data": summary
                    }
                    self.mental_model_collection.update_one(
                        {"repo_id": repo_id, "file_path": fp, "document_type": "BRIEF_FILE_OVERVIEW"},
                        {"$set": document},
                        upsert=True
                    )

            pbar.update(len(batch))
        
        pbar.close()

        # Add insights to the mental model mongodb collection
        # for insight in insights:
        #     document = {
        #         "repo_id": repo_id,
        #         "file_path": insight["file_path"],
        #         "document_type": "BRIEF_FILE_OVERVIEW",
        #         "data": insight["summary"]
        #     }
        #     self.mental_model_collection.update_one(
        #         {"repo_id": repo_id, "file_path": insight["file_path"], "document_type": "BRIEF_FILE_OVERVIEW"},
        #         {"$set": document},
        #         upsert=True
        #     )

        # Add ignored files to the mental model mongodb collection
        # for file_path in ignored:
        #     document = {
        #         "repo_id": repo_id,
        #         "file_path": file_path,
        #         "document_type": "IGNORED_FILE",
        #         "data": "IGNORE"
        #     }
        #     self.mental_model_collection.update_one(
        #         {"repo_id": repo_id, "file_path": file_path, "document_type": "IGNORED_FILE"},
        #         {"$set": document},
        #         upsert=True
        #     )
        
        # Add _cross_file_interactions_in_file in insights
        for insight in insights:
            file_path = insight["file_path"]
            dependency_info = self._cross_file_interactions_in_file(file_path, repo_id)
            insight["downstream_dep_interactions"] = dependency_info["downstream"]["interactions"]
            insight["downstream_dep_files"] = list(dependency_info["downstream"]["files"])
            insight["upstream_dep_interactions"] = dependency_info["upstream"]["interactions"]
            insight["upstream_dep_files"] = list(dependency_info["upstream"]["files"])
        
        # for insight in insights:
        #     file_path = insight["file_path"]
        #     file_dependencies = self._get_file_dependencies(file_path, repo_id)

        #     insight["downstream_dep_files"] = file_dependencies["downstream_files"]
        #     insight["upstream_dep_files"] = file_dependencies["upstream_files"]

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

    def _cross_file_interactions_in_file(self, file_path: str, repo_id: str):
        """Infer cross-file interactions for a given file by finding references to and from definitions in other files."""

        # Downstream: file_path → other files
        downstream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id, file_path: $file_path, is_reference: true})
        -[:REFERENCES]->(ident:ASTNode)
        WHERE ident.file_path <> $file_path
        MATCH (def:ASTNode)
        WHERE def.node_id = ident.parent_id
        RETURN DISTINCT ref.name AS ref_name, def.node_type AS node_type, def.file_path AS def_file_path
        """

        # Upstream: other files → file_path
        upstream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id, is_reference: true})
        -[:REFERENCES]->(ident:ASTNode {file_path: $file_path})
        MATCH (def:ASTNode)
        WHERE def.node_id = ident.parent_id
        RETURN DISTINCT ref.file_path AS ref_file_path, ref.name AS ref_name, def.node_type AS node_type
        """

        with self.neo4j_client.driver.session() as session:
            # Downstream
            downstream_result = list(session.run(downstream_query, repo_id=repo_id, file_path=file_path))
            downstream_interactions = [
                f"{record['ref_name']} REFERENCES {record['node_type']} IN {record['def_file_path']}"
                for record in downstream_result
            ]
            downstream_files = {
                record['def_file_path'] for record in downstream_result if record['def_file_path'] != file_path
            }

            # Upstream
            upstream_result = list(session.run(upstream_query, repo_id=repo_id, file_path=file_path))
            upstream_interactions = [
                f"{record['ref_name']} IN {record['ref_file_path']} REFERENCES {record['node_type']} IN {file_path}"
                for record in upstream_result
            ]
            upstream_files = {
                record['ref_file_path'] for record in upstream_result if record['ref_file_path'] != file_path
            }

            return {
                "downstream": {
                    "interactions": downstream_interactions,
                    "files": downstream_files,
                },
                "upstream": {
                    "interactions": upstream_interactions,
                    "files": upstream_files,
                },
            }
    
    def _get_file_dependencies(self, file_path: str, repo_id: str) -> Dict[str, List[str]]:
        """Get files that interact with the given file via cross-file references."""
        down_stream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id, file_path: $file_path})
                -[:DEPENDS_ON]->(ident:ASTNode)
        RETURN ident.file_path
        """

        up_stream_query = """
        MATCH (ref:ASTNode {repo_id: $repo_id})
                -[:DEPENDS_ON]->(ident:ASTNode {file_path: $file_path})
        RETURN ref.file_path
        """

        with self.neo4j_client.driver.session() as session:
            downstream_files = [record["ident.file_path"] for record in session.run(down_stream_query, repo_id=repo_id, file_path=file_path)]
            upstream_files = [record["ref.file_path"] for record in session.run(up_stream_query, repo_id=repo_id, file_path=file_path)]

        return {
            "downstream_files": downstream_files,
            "upstream_files": upstream_files,
        }
        
    
