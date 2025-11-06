from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

# Assuming these imports exist from your codebase
from .db import get_mongo_client, get_neo4j_client, get_potential_entry_points
from .llm import GroqLLM

# === Constants =================================================================
MENTAL_MODEL_COL = "mental_model"
ROLLING_SUMMARY = "ROLLING_SUMMARY_V1"  # Cumulative summary per depth
DOC_HUMAN = "REPO_ARCHITECTURE_V5"
DOC_RETR = "REPO_ARCHITECTURE_RETRIEVAL_V5"


# === Helper Functions ==========================================================

def now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


# === Core Builder ==============================================================

class RepoArchBuilderRolling:
    """
    Builds architecture overviews using rolling summary (cumulative enrichment).
    
    Approach:
    1. BFS to assign depth to each file from entry point
    2. Iterate depth by depth:
       - Fetch rolling summary from previous depth (or empty if first)
       - Enrich with current depth + next depth files
       - Store enriched rolling summary
    3. Organize final rolling summary into structured document
    """

    def __init__(self, *, mongo=None, neo=None, llm: GroqLLM = None) -> None:
        self.mongo = mongo or get_mongo_client()
        self.neo = neo or get_neo4j_client()
        self.llm = llm or GroqLLM()
        self.mental = self.mongo[MENTAL_MODEL_COL]

    # ---- Public API -----------------------------------------------------------

    async def build(self, repo_id: str) -> Dict[str, Any]:
        """
        Build architecture overviews for all entry points.
        Returns dict with 'human' and 'retrieval' lists of documents.
        """
        entry_points = get_potential_entry_points(self.mongo, repo_id) or []
        if not entry_points:
            raise ValueError(f"No entry points found for repo {repo_id}")

        human_docs = []
        retrieval_docs = []

        for ep in entry_points:
            print(f"Building architecture for entry point: {ep}")
            
            # Build rolling summary through all depths
            final_rolling_summary = await self._build_rolling_summary(repo_id, ep)
            
            # Organize into human-friendly document
            human_doc = await self._organize_final_document(
                repo_id=repo_id,
                entry_point=ep,
                rolling_summary=final_rolling_summary,
                doc_type=DOC_HUMAN,
                retrieval_mode=False
            )
            human_docs.append(human_doc)
            
            # Organize into retrieval-optimized document
            retrieval_doc = await self._organize_final_document(
                repo_id=repo_id,
                entry_point=ep,
                rolling_summary=final_rolling_summary,
                doc_type=DOC_RETR,
                retrieval_mode=True
            )
            retrieval_docs.append(retrieval_doc)

        return {
            "human": human_docs,
            "retrieval": retrieval_docs
        }

    # ---- Core Logic -----------------------------------------------------------

    async def _build_rolling_summary(self, repo_id: str, entry_point: str) -> str:
        """
        Build rolling summary by iterating through depth levels.
        Each iteration enriches understanding without losing detail.
        """
        # 1. Build depth map via BFS
        depth_map = self._build_depth_map(repo_id, entry_point)
        
        if not depth_map:
            return f"Entry point {entry_point} (no dependencies found)"
        
        # 2. Group files by depth
        files_by_depth = defaultdict(list)
        for file_path, depth in depth_map.items():
            files_by_depth[depth].append(file_path)
        
        max_depth = max(files_by_depth.keys())
        print(f"  Found {len(depth_map)} files across {max_depth + 1} depth levels")
        
        # 3. Check if rolling summary already exists (resume capability)
        existing_summary = self._get_rolling_summary(repo_id, entry_point, max_depth)
        if existing_summary:
            print(f"  Found existing rolling summary at depth {max_depth}, using it")
            return existing_summary
        
        # 4. Iterate through depths, enriching at each level
        rolling_summary = None
        
        for depth in range(max_depth + 1):
            print(f"  Processing depth {depth}/{max_depth}")
            
            # Check if we have a summary for this depth already
            existing = self._get_rolling_summary(repo_id, entry_point, depth)
            if existing:
                print(f"    Using cached rolling summary for depth {depth}")
                rolling_summary = existing
                continue
            
            # Get files at current depth
            current_files = files_by_depth.get(depth, [])
            
            # Get files at next depth (dependencies of current files)
            next_files = files_by_depth.get(depth + 1, [])
            
            if not current_files:
                print(f"    No files at depth {depth}, skipping")
                continue
            
            # Fetch BRIEF_FILE_OVERVIEW for current and next depth
            current_briefs = self._fetch_briefs_batch(repo_id, current_files)
            next_briefs = self._fetch_briefs_batch(repo_id, next_files) if next_files else []
            
            # Enrich rolling summary
            rolling_summary = await self._enrich_rolling_summary(
                rolling_summary=rolling_summary,
                current_depth=depth,
                current_briefs=current_briefs,
                next_briefs=next_briefs,
                is_entry_point=(depth == 0),
                entry_point_path=entry_point
            )
            
            # Store in DB
            self._store_rolling_summary(repo_id, entry_point, depth, rolling_summary)
        
        return rolling_summary or f"Entry point {entry_point}"

    def _build_depth_map(self, repo_id: str, entry_point: str) -> Dict[str, int]:
        """
        BFS from entry point to assign depth to each reachable file.
        Returns dict of {file_path: depth}
        """
        depth_map = {entry_point: 0}
        queue = [(entry_point, 0)]
        visited = {entry_point}
        
        while queue:
            file_path, depth = queue.pop(0)
            
            # Get downstream dependencies
            try:
                _, downstream = self.neo.file_dependencies(
                    file_path=file_path,
                    repo_id=repo_id
                )
                
                for child in downstream:
                    if child not in visited:
                        visited.add(child)
                        depth_map[child] = depth + 1
                        queue.append((child, depth + 1))
            except Exception as e:
                print(f"    Warning: Could not fetch dependencies for {file_path}: {e}")
                continue
        
        return depth_map

    def _fetch_briefs_batch(self, repo_id: str, file_paths: List[str]) -> List[Dict[str, str]]:
        """
        Fetch BRIEF_FILE_OVERVIEW for a batch of files.
        Returns list of {file_path, brief_text} dicts.
        """
        result = []
        for file_path in file_paths:
            doc = self.mental.find_one(
                {
                    "repo_id": repo_id,
                    "document_type": "BRIEF_FILE_OVERVIEW",
                    "file_path": file_path
                },
                {"_id": 0, "data": 1}
            )
            brief = (doc or {}).get("data", "")
            if brief:
                result.append({
                    "file_path": file_path,
                    "brief": brief
                })
        return result

    async def _enrich_rolling_summary(
        self,
        rolling_summary: Optional[str],
        current_depth: int,
        current_briefs: List[Dict[str, str]],
        next_briefs: List[Dict[str, str]],
        is_entry_point: bool,
        entry_point_path: str
    ) -> str:
        """
        Enrich the rolling summary with information from current and next depth.
        This is ENRICHMENT, not summarization - we keep ALL details.
        """
        system_prompt = """You are building a cumulative, enriched understanding of a codebase.

            CRITICAL: This is NOT summarization. This is ENRICHMENT.

            Your job:
            - Keep ALL details from the rolling summary (if provided)
            - Add details about how current files use their dependencies
            - Explain connections and interactions clearly
            - Include specific file paths in backticks
            - Be thorough - don't drop any information

            Think of it like adding layers to a painting - each layer adds detail without removing what's underneath.

            Your output will become the new rolling summary for the next iteration.
            Remember, the output should clearly explain what the components do and how they interact.
            """

        # Build context for current depth files
        current_context = "\n\n".join([
            f"### {item['file_path']}\n{item['brief']}"
            for item in current_briefs
        ])
        
        # Build context for next depth files (dependencies)
        next_context = ""
        if next_briefs:
            next_context = "\n\n".join([
                f"### {item['file_path']}\n{item['brief']}"
                for item in next_briefs
            ])
        
        # Construct user prompt
        if is_entry_point:
            user_prompt = f"""This is the FIRST iteration - we're analyzing the entry point.

                ENTRY POINT: `{entry_point_path}`
                {current_context}

                """
            if next_briefs:
                user_prompt += f"""IMMEDIATE DEPENDENCIES:
                    {next_context}

                    """
            user_prompt += f"""Task: Create the initial understanding. Explain:
                - What does the entry point `{entry_point_path}` do?
                - How does it use its immediate dependencies?
                - Include ALL relevant details with file paths in backticks."""
        
        else:
            user_prompt = f"""ROLLING SUMMARY SO FAR (keep ALL of this):
                {rolling_summary}

                ---

                DETAILED OVERVIEW OF THE CURRENT FILES:
                {current_context}

                """
            if next_briefs:
                user_prompt += f"""---

                    DETAILED OVERVIEW OF DEPENDENCIES:
                    {next_context}

                    """
            user_prompt += f"""---

                Task: Enrich the understanding by adding details about:
                - What current files do
                - How they use their dependencies
                - Connections between files

                Keep ALL information from rolling summary and add new context.
                Remember, the purpose is to clearly explain how the codebase works and how components interact."""

        try:
            output = await self.llm.generate_async(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.0,
                reasoning_effort="medium",
            )
            return output.strip()
        except Exception as e:
            print(f"    Error enriching rolling summary at depth {current_depth}: {e}")
            # Fallback: concatenate what we have
            if rolling_summary:
                return rolling_summary + f"\n\n(Depth {current_depth} enrichment failed)"
            else:
                return f"Entry point at depth {current_depth} (enrichment failed)"

    def _get_rolling_summary(self, repo_id: str, entry_point: str, depth: int) -> Optional[str]:
        """Get rolling summary for a specific depth from DB."""
        doc = self.mental.find_one(
            {
                "repo_id": repo_id,
                "document_type": ROLLING_SUMMARY,
                "entry_point": entry_point,
                "depth": depth
            },
            {"_id": 0}
        )
        return doc.get("summary") if doc else None

    def _store_rolling_summary(self, repo_id: str, entry_point: str, depth: int, summary: str) -> None:
        """Store rolling summary for a specific depth in DB."""
        doc = {
            "repo_id": repo_id,
            "document_type": ROLLING_SUMMARY,
            "entry_point": entry_point,
            "depth": depth,
            "summary": summary,
            "generated_at": now_iso()
        }
        
        key = {
            "repo_id": repo_id,
            "document_type": ROLLING_SUMMARY,
            "entry_point": entry_point,
            "depth": depth
        }
        
        self.mental.update_one(key, {"$set": doc}, upsert=True)

    # ---- Final Organization ---------------------------------------------------

    async def _organize_final_document(
        self,
        repo_id: str,
        entry_point: str,
        rolling_summary: str,
        doc_type: str,
        retrieval_mode: bool
    ) -> Dict[str, Any]:
        """
        Organize the final rolling summary into a structured document.
        This is ORGANIZATION only - no compression.
        """
        if retrieval_mode:
            system_prompt = """You are organizing a detailed codebase understanding into a retrieval-optimized document.

            CRITICAL: The rolling summary contains ALL the details. DO NOT compress or lose information.
            Your job: Organize into a structured format for AI agent navigation.

            Required sections:
            1. **Overview** - High-level purpose (2-3 paragraphs from the rolling summary)
            2. **Architecture Components** - Identify and list logical components with their files
            3. **Architecture Patterns** - What patterns are evident in the structure
            4. **Key Flows** - Trace important execution paths with file sequences
            5. **Data & External Systems** - Databases, APIs, queues, config

            Style:
            - Dense, path-heavy format with many inline file paths in backticks
            - Use lists and short paragraphs
            - Include navigation markers (what invokes what, what reads/writes what)
            - Keep ALL technical details from the rolling summary

            Output clean, well-structured markdown."""
        else:
            system_prompt = """You are organizing a detailed codebase understanding into a human-friendly document.

            CRITICAL: The rolling summary contains ALL the details. DO NOT compress or lose information.
            Your job: Organize into a structured format that helps newcomers understand.

            Required sections:
            1. **Overview** - High-level purpose (2-3 paragraphs from the rolling summary)
            2. **Architecture Components** - Identify and describe logical components
            3. **Architecture Patterns** - What patterns are evident in the structure
            4. **Key Flows** - Trace important execution paths as narratives
            5. **Data & External Systems** - Databases, APIs, queues, config

            Style:
            - Clear narrative that tells the story of how the code works
            - Include file paths in backticks for reference
            - Use proper markdown with sections and subsections
            - Keep ALL technical details from the rolling summary

            Output clean, well-structured markdown."""

        user_prompt = f"""Entry Point: {entry_point}

            Here is the complete, enriched understanding of this codebase:

            {rolling_summary}

            ---

            Organize this into the required 5-section structure. 
            Keep all details - just add structure and organization."""

        try:
            architecture = await self.llm.generate_async(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.0,
                reasoning_effort="medium",
            )
        except Exception as e:
            print(f"  Error organizing final document: {e}")
            # Fallback: use rolling summary as-is with basic structure
            architecture = f"# {entry_point}\n\n## Complete Understanding\n\n{rolling_summary}"
        
        # Create and persist document
        doc = {
            "repo_id": repo_id,
            "document_type": doc_type,
            "entry_point": entry_point,
            "architecture": architecture.strip(),
            "generated_at": now_iso(),
        }
        
        key = {
            "repo_id": repo_id,
            "document_type": doc_type,
            "entry_point": entry_point
        }
        
        self.mental.update_one(key, {"$set": doc}, upsert=True)
        
        return doc


# === Convenience Functions =====================================================

async def build_repo_architecture_v2(repo_id: str) -> Dict[str, Any]:
    """Build architecture overviews using rolling summary approach."""
    builder = RepoArchBuilderRolling()
    return await builder.build(repo_id)


async def get_repo_architecture(repo_id: str, entry_point: str = None) -> Dict[str, Any]:
    """
    Get human-friendly architecture overview.
    If entry_point is None, returns all entry points.
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]
    
    if entry_point:
        doc = mental.find_one(
            {
                "repo_id": repo_id,
                "document_type": DOC_HUMAN,
                "entry_point": entry_point
            },
            {"_id": 0}
        )
        if not doc:
            raise ValueError(f"No architecture found for {repo_id}:{entry_point}")
        return doc
    else:
        docs = list(mental.find(
            {"repo_id": repo_id, "document_type": DOC_HUMAN},
            {"_id": 0}
        ))
        if not docs:
            raise ValueError(f"No architecture found for repo {repo_id}")
        return {"entry_points": docs}


async def get_repo_architecture_retrieval(repo_id: str, entry_point: str = None) -> Dict[str, Any]:
    """
    Get retrieval-optimized architecture overview.
    If entry_point is None, returns all entry points.
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]
    
    if entry_point:
        doc = mental.find_one(
            {
                "repo_id": repo_id,
                "document_type": DOC_RETR,
                "entry_point": entry_point
            },
            {"_id": 0}
        )
        if not doc:
            raise ValueError(f"No retrieval doc found for {repo_id}:{entry_point}")
        return doc
    else:
        docs = list(mental.find(
            {"repo_id": repo_id, "document_type": DOC_RETR},
            {"_id": 0}
        ))
        if not docs:
            raise ValueError(f"No retrieval docs found for repo {repo_id}")
        return {"entry_points": docs}