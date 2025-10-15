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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Set, Any


from bson import ObjectId

from .core.base import PipelineStage, StageResult
from ..models.data_model import S3StorageInfo
from ..db import Neo4jClient, get_neo4j_client, get_mongo_client
from ..llm import GroqLLM
from ..repo_arch_service import build_repo_architecture


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
            # repo_summary = await self.generate_repo_summary(insights, repo_id)
            await self.gen_repo_summary_from_file_overview(repo_id)
            await self._set_potential_entry_points(insights, repo_id)
            await build_repo_architecture(repo_id)

            # self._write_to_md(insights, [], repo_id, dir_tree)
            # # self._write_to_jsonl(insights, file_summaries, repo_id, dir_tree)
            # self._write_to_jsonl_v2(insights, repo_id)

            # await attention_db_runtime.build_keybank(repo_id)

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
        
    async def gen_repo_summary_from_file_overview(self, repo_id: str) -> str:
        """Generate a repo summary from existing BRIEF_FILE_OVERVIEW documents in MongoDB.
        Produces a single summary + a validated list of entry points (subset of repo files).
        Handles large corpora via map-reduce with strict char budgets per LLM call.
        """
        import asyncio
        import json
        from datetime import datetime, timezone

        # ---- constants & helpers ----
        # 1 token ≈ 4 chars. Keep well under 80k chars to include prompt & JSON headroom.
        MAX_CHARS_PER_CALL = 75000
        MAP_CONCURRENCY = 4  # parallelism for map stage

        # Check MongoDB for existing REPO_SUMMARY. if exists, skip.
        existing_summary = self.mental_model_collection.find_one(
            {"repo_id": repo_id, "document_type": "REPO_SUMMARY"}
        )
        if existing_summary:
            print(f"Job {self.job_id}: REPO_SUMMARY already exists for repo {repo_id}, skipping write.")
            return existing_summary["data"], existing_summary["entry_points"]

        def serialize_pairs(pairs):
            # Each item as:
            # FILE: <path>\nSUMMARY: <summary>\n---
            # Keep it simple and deterministic.
            lines = []
            for p in pairs:
                fp = p["file_path"]
                sm = (p["summary"] or "").strip()
                lines.append(f"FILE: {fp}\nSUMMARY: {sm}\n---")
            return "\n".join(lines)

        def chunk_pairs(pairs, max_chars=MAX_CHARS_PER_CALL, overhead=15000):
            """Greedy packing into chunks under (max_chars - overhead)."""
            chunks = []
            cur = []
            cur_len = 0
            limit = max_chars - overhead
            for p in pairs:
                item_txt = f"FILE: {p['file_path']}\nSUMMARY: {(p['summary'] or '').strip()}\n---\n"
                if cur and cur_len + len(item_txt) > limit:
                    chunks.append(cur)
                    cur = []
                    cur_len = 0
                cur.append(p)
                cur_len += len(item_txt)
            if cur:
                chunks.append(cur)
            return chunks

        async def llm_map_chunk(chunk_pairs_list):
            """Call LLM on one chunk and return parsed JSON with chunk_summary & candidates."""
            system_prompt = (
                "Repository name: " + repo_id + "\n\n"
                "You are summarizing a subset of repository files.\n"
                "Given file-path + brief summaries, produce:\n"
                "1) 'chunk_summary': 5–10 concise bullets that capture what these files collectively do.\n"
                "2) 'candidate_entry_points': only file paths from THIS CHUNK whose summaries clearly indicate they start/launch/boot/serve as CLI.\n"
                "   - Base this ONLY on the summaries provided.\n"
                "Return STRICT JSON: {\"chunk_summary\": [...], \"candidate_entry_points\": [..]}"
            )
            # Serialize only this chunk
            serialized = serialize_pairs(chunk_pairs_list)
            user_prompt = (
                "FILES & SUMMARIES:\n"
                "<<<\n" + serialized + "\n>>>\n\n"
                "Return STRICT JSON with keys: chunk_summary (list of strings) and candidate_entry_points (list of file paths from this chunk)."
            )
            out = await self.llm_client.generate_async(
                prompt=user_prompt,
                system_prompt=system_prompt,
                reasoning_effort="medium",
                temperature=0.0,
            )
            text = (out or "").strip()
            # Parse tolerant
            try:
                obj = json.loads(text)
            except Exception:
                # try to extract first {...}
                import re
                m = re.search(r"\{.*\}", text, flags=re.DOTALL)
                obj = json.loads(m.group(0)) if m else {"chunk_summary": [], "candidate_entry_points": []}
            # Normalize
            chunk_summary = obj.get("chunk_summary") or []
            if isinstance(chunk_summary, str):
                chunk_summary = [chunk_summary]
            candidates = [c for c in (obj.get("candidate_entry_points") or []) if isinstance(c, str)]
            return {"chunk_summary": chunk_summary, "candidate_entry_points": candidates}

        async def map_stage(pairs):
            """Process all chunks in parallel with bounded concurrency."""
            chunks = chunk_pairs(pairs)
            sem = asyncio.Semaphore(MAP_CONCURRENCY)
            results = []

            async def run_one(ch):
                async with sem:
                    return await llm_map_chunk(ch)

            tasks = [asyncio.create_task(run_one(ch)) for ch in chunks]
            for t in asyncio.as_completed(tasks):
                results.append(await t)
            return results

        def build_reduce_docs(map_results):
            """Flatten map outputs into reduce-ready docs."""
            all_bullets = []
            all_candidates = []
            for r in map_results:
                all_bullets.extend(r.get("chunk_summary", []))
                all_candidates.extend(r.get("candidate_entry_points", []))
            return all_bullets, all_candidates

        async def reduce_once(bullets, candidates, allowed_file_paths):
            """One reduce pass: compress bullets and reconcile candidates."""
            import textwrap
            # Prepare text; pack into budget
            bullet_text = "\n".join(f"- {b}" for b in bullets)
            # If too long, chunk bullets and summarize again
            blocks = []
            cur = []
            cur_len = 0
            for b in bullets:
                line = f"- {b}\n"
                if cur_len + len(line) > (MAX_CHARS_PER_CALL - 3000):
                    blocks.append(cur)
                    cur = []
                    cur_len = 0
                cur.append(b)
                cur_len += len(line)
            if cur:
                blocks.append(cur)

            # If multiple blocks, summarize each block, then summarize the summaries.
            if len(blocks) > 1:
                block_summaries = []
                for blk in blocks:
                    sys = (
                        "You are compressing bullet points about a codebase into 5–8 bullets, preserving core themes.\n"
                        "Return strict JSON: {\"bullets\": [..]}"
                    )
                    usr = "BULLETS:\n" + "\n".join(f"- {x}" for x in blk) + "\n\nReturn JSON with key 'bullets'."
                    out = await self.llm_client.generate_async(
                        prompt=usr, system_prompt=sys, reasoning_effort="low", temperature=0.0
                    )
                    try:
                        obj = json.loads(out)
                        block_summaries.extend(obj.get("bullets", []))
                    except Exception:
                        block_summaries.extend(blk[:8])  # fallback
                bullets = block_summaries

            # Now final reduce: produce final summary + entry points
            allowed_paths_text = "\n".join(sorted(allowed_file_paths))
            sys_final = (
                "You are producing a single, clear repository summary and confirming entry points.\n"
                "Rules:\n"
                "- Use only the provided bullets (they came from file summaries) to write a ~300–400 word overview.\n"
                "- For entry points, choose ONLY from the ALLOWED_PATHS list and ONLY if bullets imply they start/launch/serve/CLI/etc.\n"
                "- Return STRICT JSON with keys: 'repo_summary' (string) and 'entry_points' (list of strings from ALLOWED_PATHS)."
            )
            usr_final = (
                "BULLETS:\n" + "\n".join(f"- {b}" for b in bullets) + "\n\n"
                "CANDIDATE ENTRY POINT HINTS (from earlier chunks):\n" + "\n".join(f"- {c}" for c in set(candidates)) + "\n\n"
                "ALLOWED_PATHS (must choose entry points only from this list):\n" + allowed_paths_text + "\n\n"
                "Return STRICT JSON with keys 'repo_summary' and 'entry_points'."
            )
            final_out = await self.llm_client.generate_async(
                prompt=usr_final,
                system_prompt=sys_final,
                reasoning_effort="medium",
                temperature=0.0,
            )
            final_text = (final_out or "").strip()
            try:
                final_obj = json.loads(final_text)
            except Exception:
                import re
                m = re.search(r"\{.*\}", final_text, flags=re.DOTALL)
                final_obj = json.loads(m.group(0)) if m else {"repo_summary": "", "entry_points": []}

            repo_summary = (final_obj.get("repo_summary") or "").strip()
            eps = [e for e in (final_obj.get("entry_points") or []) if e in allowed_file_paths]
            # Deduplicate preserving order
            seen = set()
            entry_points = [e for e in eps if not (e in seen or seen.add(e))]
            return repo_summary, entry_points

        # ---- main body ----
        insights_cursor = self.mental_model_collection.find(
            {"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW"},
            {"_id": 0, "file_path": 1, "data": 1}
        )
        insights = list(insights_cursor)
        if not insights:
            print(f"Job {getattr(self, 'job_id', 'NA')}: No BRIEF_FILE_OVERVIEW documents found for repo {repo_id}")
            return ""

        pairs = [{"file_path": d["file_path"], "summary": d["data"]} for d in insights]
        allowed_paths = [p["file_path"] for p in pairs]

        # MAP
        map_results = await map_stage(pairs)

        # REDUCE (maybe multi-pass)
        bullets, candidates = build_reduce_docs(map_results)

        # If bullets are excessive, the reduce_once handles re-chunking internally.
        final_summary, entry_points = await reduce_once(bullets, candidates, allowed_paths)

        # Persist to Mongo
        document = {
            "repo_id": repo_id,
            "document_type": "REPO_SUMMARY",
            "data": final_summary,
            "entry_points": entry_points,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "method": "map_reduce_from_file_overviews_v1"
        }
        self.mental_model_collection.update_one(
            {"repo_id": repo_id, "document_type": "REPO_SUMMARY"},
            {"$set": document},
            upsert=True
        )

        return final_summary, entry_points

    async def _set_potential_entry_points(self, insights: List[dict], repo_id: str) -> List[str]:
        """Get potential entry points for the repo based on insights."""
        potential_entry_points = set()
        cross_file_ref_counts = {}

        insights_files = {insight.get("file_path") for insight in insights}

        # Initially add all files to potential_entry_counts
        for insight in insights:
            file_path = insight.get("file_path")
            potential_entry_points.add(file_path)

        for insight in insights:
            # Extract file paths from insights
            file_path = insight.get("file_path")
            upstream_dep_files = insight.get("upstream_dep_files", {})
            # print(f"File: {file_path} has upstream deps: {upstream_dep_files}")

            # cross_file_ref_counts[file_path] = len(cross_file_paths)

            for upstream_file in upstream_dep_files:
                if upstream_file in insights_files and file_path in potential_entry_points:
                    potential_entry_points.remove(file_path)


            # for cross_file_path in cross_file_paths :
            #     if cross_file_path in potential_entry_points and cross_file_path not in insights_files:
            #         potential_entry_points.remove(cross_file_path)

        # print(f"Potential entry points: {potential_entry_points}")
        # print(f"Cross-file reference counts: {cross_file_ref_counts}")
        # if not potential_entry_points:
        #     if cross_file_ref_counts:
        #         # Find the minimum number of references
        #         min_refs = min(cross_file_ref_counts.values())
        #         # Add files with the minimum number of references to potential entry points
        #         potential_entry_points.update(
        #             file_path for file_path, ref_count in cross_file_ref_counts.items()
        #             if ref_count == min_refs
        #         )
        
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

            system_prompt = (
                "Given the repo_name, file_path, and its code — your task is to write a detailed explanation "
                "of what the code file does, describing its purpose, logic, and functionality in depth. "
                "If the file is not critical to understanding the overall behaviour of the code repository, "
                "simply output \"IGNORE\".\n\n"
                "Ignore:\n"
                "- tutorial example files,\n"
                "- tests,\n"
                "- docs.\n\n"
                "While analyzing the code, also consider where in the repository the file is located."
            )

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
            dependency_info = self._cross_file_interactions_in_file(file_path, repo_id)
            insight["downstream_dep_interactions"] = dependency_info["downstream"]["interactions"]
            insight["downstream_dep_files"] = list(dependency_info["downstream"]["files"])
            insight["upstream_dep_interactions"] = dependency_info["upstream"]["interactions"]
            insight["upstream_dep_files"] = list(dependency_info["upstream"]["files"])

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

            dependency_info = self._cross_file_interactions_in_file(file_path, repo_id)
            cross_refs = "\n".join(dependency_info["downstream"]["interactions"]) if dependency_info["downstream"]["interactions"] else "No cross-file dependencies."

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
                    dependency_info = self._cross_file_interactions_in_file(file_path, repo_id)
                    file_info = {
                        "path": file_path,
                        "summary": result or "No summary available.",
                        "cross_file_interactions": dependency_info["downstream"]["interactions"],
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