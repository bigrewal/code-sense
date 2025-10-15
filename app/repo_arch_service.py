# repo_arch_service.py

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

from .db import get_mongo_client, get_neo4j_client, get_entry_point_files
from .llm import GroqLLM

MENTAL_MODEL_COL = "mental_model"
MAX_CHARS = 140000  # ~35k tokens safe buffer


async def build_repo_architecture(repo_id: str, max_depth: int = 5):
    """
    Build a high-level architecture overview of the repo using file summaries + dependency info.

    Produces TWO text artifacts:
      1) Human-readable architecture (document_type="REPO_ARCHITECTURE")
      2) Retrieval-optimized architecture (document_type="REPO_ARCHITECTURE_RETRIEVAL")
         - Formatted like the human-readable architecture, but enriched with explicit pointers
           (exact file paths, entry points, dependency edges) to help an LLM Q/A agent.

    IMPORTANT BEHAVIOR:
    - Only call the LLM to (re)generate a document if that specific document does not already
      exist in MongoDB.
    - If both already exist, return the existing human-readable doc immediately.
    """

    mongo = get_mongo_client()
    neo = get_neo4j_client()
    mental = mongo[MENTAL_MODEL_COL]
    llm = GroqLLM()

    # --- 0. Check existing docs and short-circuit when possible ---
    existing_human = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE"},
        {"_id": 0},
    )
    existing_retrieval = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE_RETRIEVAL"},
        {"_id": 0},
    )

    if existing_human and existing_retrieval:
        return existing_human

    need_human = existing_human is None
    need_retrieval = existing_retrieval is None

    # If neither is needed, return early (defensive; covered above)
    if not (need_human or need_retrieval):
        return existing_human

    # --- 1. Load entry points and file overviews (only if we need to generate something) ---
    entry_points = get_entry_point_files(mongo, repo_id) or []
    if not entry_points:
        raise ValueError(f"No entry points found for repo {repo_id}")

    all_files_cursor = mental.find(
        {"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW"},
        {"_id": 0, "file_path": 1, "data": 1},
    )
    file_summaries = {doc["file_path"]: doc["data"] for doc in all_files_cursor}
    if not file_summaries:
        raise ValueError(f"No file summaries found for repo {repo_id}")

    # --- 2. BFS traversal from entry points ---
    visited: Set[str] = set()
    traversal_order: List[str] = []
    queue: List[Tuple[str, int]] = [(fp, 0) for fp in entry_points]

    while queue:
        file_path, level = queue.pop(0)
        if file_path in visited or level > max_depth:
            continue
        visited.add(file_path)
        traversal_order.append(file_path)

        try:
            _, downstream = neo.file_dependencies(repo_id=repo_id, file_path=file_path)
        except Exception:
            downstream = []
        for child in downstream:
            if child not in visited:
                queue.append((child, level + 1))

    # --- 3. Prepare token-bounded chunks (used by both outputs) ---
    chunks: List[List[str]] = []
    current_chunk: List[str] = []
    current_size = 0

    for file_path in traversal_order:
        brief = file_summaries.get(file_path, "No summary available.")
        try:
            upstream, downstream = neo.file_dependencies(repo_id=repo_id, file_path=file_path)
        except Exception:
            upstream, downstream = [], []

        text_block = (
            f"FILE: {file_path}\n"
            f"SUMMARY: {brief}\n"
            f"UPSTREAM: {upstream}\n"
            f"DOWNSTREAM: {downstream}\n"
            "---\n"
        )
        if current_size + len(text_block) > MAX_CHARS:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(text_block)
        current_size += len(text_block)

    if current_chunk:
        chunks.append(current_chunk)

    # Helper to summarize a chunk (shared by both generations)
    async def summarize_chunk(idx: int, chunk: List[str]) -> str:
        content = "\n".join(chunk)
        system_prompt = (
            "You are analyzing part of a software repository.\n"
            "Each file has a brief summary and its dependencies.\n"
            "Your task: describe the architecture patterns, roles, and component structure suggested by these files.\n"
            "Keep this self-contained, concise, and factual. Avoid redundancy."
        )
        user_prompt = (
            f"Repo: {repo_id}\n\n"
            f"Chunk {idx} of {len(chunks)}\n\n"
            f"{content}\n\n"
            "Write a concise architecture summary of this chunk."
        )
        result = await llm.generate_async(
            prompt=user_prompt,
            system_prompt=system_prompt,
            reasoning_effort="medium",
            temperature=0.0,
        )
        return (result or "").strip()

    summaries: List[str] = []

    # We only need to perform LLM chunk summarization if at least one of the documents is missing.
    if need_human or need_retrieval:
        summaries = await asyncio.gather(*(summarize_chunk(i, c) for i, c in enumerate(chunks)))

    # --- 4. Generate missing human-readable architecture (LLM ONLY IF NEEDED) ---
    architecture_text = existing_human["architecture"] if existing_human else ""

    if need_human:
        combined_input = "\n\n".join(
            f"PART {i+1}:\n{summary}" for i, summary in enumerate(summaries)
        )
        final_system_prompt = (
            "You are a senior software architect.\n"
            "Given multiple partial architecture analyses of a repository, produce one coherent, non-redundant, clear architecture overview.\n"
            "Focus on how major components interact, key data/control flows, and entry points.\n"
            "Keep it professional and readable."
            "Also include a brief summary of the repo at the top."
        )
        final_prompt = (
            f"Repository ID: {repo_id}\n\n"
            f"Partial architecture summaries:\n{combined_input}\n\n"
            "Now write a unified architecture overview for someone who has not seen the codebase before."
        )

        final_summary = await llm.generate_async(
            prompt=final_prompt,
            system_prompt=final_system_prompt,
            reasoning_effort="medium",
            temperature=0.0,
        )
        architecture_text = (final_summary or "").strip()

        # Persist human-readable doc
        doc_human = {
            "repo_id": repo_id,
            "document_type": "REPO_ARCHITECTURE",
            "entry_points": entry_points,
            "architecture": architecture_text,
            "num_chunks": len(chunks),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "max_depth": max_depth,
        }
        mental.update_one(
            {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE"},
            {"$set": doc_human},
            upsert=True,
        )

    # --- 5. Generate missing retrieval-optimized text (LLM ONLY IF NEEDED) ---
    if need_retrieval:
        # Build compact, factual context to enrich with pointers.
        dep_up: Dict[str, List[str]] = {}
        dep_down: Dict[str, List[str]] = {}
        for fp in traversal_order:
            try:
                up, down = neo.file_dependencies(repo_id=repo_id, file_path=fp)
            except Exception:
                up, down = [], []
            dep_up[fp] = up
            dep_down[fp] = down

        retrieval_context = (
            f"REPO_ID: {repo_id}\n"
            f"ENTRY_POINTS: {json.dumps(entry_points)}\n"
            f"TRAVERSAL_ORDER: {json.dumps(traversal_order)}\n"
            f"DEPENDENCY_UPSTREAM: {json.dumps(dep_up)}\n"
            f"DEPENDENCY_DOWNSTREAM: {json.dumps(dep_down)}\n"
            f"PARTIAL_SUMMARIES: {json.dumps(summaries)}\n"
            f"EXISTING_HUMAN_OVERVIEW: {architecture_text}\n"
        )

        # NOTE: This is intentionally formatted like the human-readable overview (no strict markdown sections),
        # but it adds explicit pointers (exact file paths and dependency hints) inline to aid an LLM Q/A agent.
        retrieval_system_prompt = (
            "You are producing a human-readable architecture overview intended to help an LLM Q/A agent route queries.\n"
            "Write in the same narrative style as a normal architecture overview (short paragraphs and brief bullet points only if helpful), "
            "NOT a rigid index and NOT strict markdown sections.\n"
            "Enrich the text with precise pointers to where things live:\n"
            "- Always mention exact file paths in parentheses when referring to components (e.g., (see: api/server.py)).\n"
            "- Call out entry points explicitly using the provided list.\n"
            "- When useful, note simple dependency hints like 'reads from X' or 'invokes Y', referencing file paths.\n"
            "Constraints: Be factual; do not invent files or components beyond the provided context."
            "Also include a brief summary of the repo at the top."
        )
        retrieval_user_prompt = (
            "Using ONLY the context below, write a human-readable architecture overview that mirrors the tone and structure of the standard overview, "
            "but adds inline pointers (exact file paths and simple dependency hints) so a Q/A agent can quickly jump to the right places.\n\n"
            f"{retrieval_context}\n"
        )

        retrieval_text = await llm.generate_async(
            prompt=retrieval_user_prompt,
            system_prompt=retrieval_system_prompt,
            reasoning_effort="medium",
            temperature=0.0,
        )
        retrieval_text = (retrieval_text or "").strip()

        doc_retrieval_text = {
            "repo_id": repo_id,
            "document_type": "REPO_ARCHITECTURE_RETRIEVAL",
            "entry_points": entry_points,
            "architecture": retrieval_text,
            "num_chunks": len(chunks),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "max_depth": max_depth,
            "source": "repo_arch_service.build_repo_architecture:retrieval-prose-v2",
        }
        mental.update_one(
            {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE_RETRIEVAL"},
            {"$set": doc_retrieval_text},
            upsert=True,
        )

    # Return the (existing or newly generated) human-readable doc
    if existing_human:
        return existing_human
    else:
        # We just generated and stored it above
        return mental.find_one(
            {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE"},
            {"_id": 0},
        )


async def get_repo_architecture(repo_id: str) -> Dict[str, Any]:
    """Retrieve the stored human-readable architecture overview for a given repo_id."""
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]
    doc = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE"},
        {"_id": 0},
    )
    if not doc:
        raise ValueError(f"No architecture overview found for repo {repo_id}")
    return doc


async def get_repo_architecture_retrieval(repo_id: str) -> Dict[str, Any]:
    """
    Retrieve the stored retrieval-optimized architecture for a given repo_id.

    NOTE: This text is formatted like the human-readable overview, but enriched with
    inline pointers (exact file paths, entry points, and simple dependency hints)
    to make downstream LLM Q/A more effective.
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]
    doc = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE_RETRIEVAL"},
        {"_id": 0},
    )
    if not doc:
        raise ValueError(f"No retrieval-optimized architecture found for repo {repo_id}")
    return doc
