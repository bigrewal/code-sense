# repo_arch_service.py

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from .db import get_mongo_client, get_neo4j_client, get_entry_point_files
from .llm import GroqLLM

MENTAL_MODEL_COL = "mental_model"
MAX_CHARS = 75000  # ~20k tokens safe buffer


async def build_repo_architecture(repo_id: str, max_depth: int = 3) -> Dict[str, Any]:
    """
    Build a high-level architecture overview of the repo using file summaries + dependency info.
    - Traverses from entry points (breadth-first) up to `max_depth`.
    - Combines file summaries and dependency info until near token limit, then creates another chunk.
    - Processes all chunks asynchronously via LLM.
    - Combines their responses into a single coherent architecture via a final LLM request.
    - Saves architecture in MongoDB under document_type="REPO_ARCHITECTURE".
    """

    mongo = get_mongo_client()
    neo = get_neo4j_client()
    mental = mongo[MENTAL_MODEL_COL]
    llm = GroqLLM()

    ## Check if architecture already exists
    existing = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE"},
        {"_id": 0},
    )
    if existing:
        return existing


    # --- 1. Get entry points and brief file overviews ---
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

    # --- 2. Build BFS traversal from entry points ---
    visited: Set[str] = set()
    traversal_order: List[str] = []
    queue: List[tuple[str, int]] = [(fp, 0) for fp in entry_points]

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

    # --- 3. Prepare token-bounded chunks ---
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

    # --- 4. Process chunks asynchronously via LLM ---
    async def summarize_chunk(idx: int, chunk: List[str]) -> str:
        content = "\n".join(chunk)
        system_prompt = (
            "You are analyzing part of a software repository.\n"
            "Each file has a brief summary and its dependencies.\n"
            "Your task: describe the architecture patterns, roles, and component structure suggested by these files.\n"
            "Keep this self-contained, concise, and factual. Avoid redundancy."
        )
        user_prompt = f"Repo: {repo_id}\n\nChunk {idx} of {len(chunks)}\n\n{content}\n\nWrite a concise architecture summary of this chunk."
        result = await llm.generate_async(
            prompt=user_prompt,
            system_prompt=system_prompt,
            reasoning_effort="medium",
            temperature=0.0,
        )
        return (result or "").strip()

    summaries = await asyncio.gather(*(summarize_chunk(i, chunk) for i, chunk in enumerate(chunks)))

    # --- 5. Combine all partial summaries into a final coherent overview ---
    combined_input = "\n\n".join(
        f"PART {i+1}:\n{summary}" for i, summary in enumerate(summaries)
    )
    final_system_prompt = (
        "You are a senior software architect.\n"
        "Given multiple partial architecture analyses of a repository, produce one coherent, non-redundant, clear architecture overview.\n"
        "Focus on how major components interact, key data/control flows, and entry points.\n"
        "Keep it under 600 words, professional, and readable."
    )
    final_prompt = f"Repository ID: {repo_id}\n\nPartial architecture summaries:\n{combined_input}\n\nNow write a unified architecture overview for someone who has not seen the codebase before."

    final_summary = await llm.generate_async(
        prompt=final_prompt,
        system_prompt=final_system_prompt,
        reasoning_effort="medium",
        temperature=0.0,
    )

    architecture_text = (final_summary or "").strip()

    # --- 6. Persist result to Mongo ---
    doc = {
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
        {"$set": doc},
        upsert=True,
    )

    return doc


async def get_repo_architecture(repo_id: str) -> Dict[str, Any]:
    """Retrieve the stored architecture overview for a given repo_id."""
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]
    doc = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE"},
        {"_id": 0},
    )
    if not doc:
        raise ValueError(f"No architecture overview found for repo {repo_id}")
    return doc