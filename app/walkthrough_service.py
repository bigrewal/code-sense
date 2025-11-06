# walkthrough_service.py

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Set

from .db import (
    get_mongo_client,
    get_entry_point_files,
    get_repo_summary,
    get_neo4j_client,
    Neo4jClient
)
from .llm import GroqLLM
from .tools import fetch_code_file


# ---- Collections ----
MENTAL_MODEL_COL = "mental_model"              # BRIEF_FILE_OVERVIEW docs live here
WALK_SESSIONS_COL = "walkthrough_sessions"     # per-repo walkthrough session state
WALK_NUGGETS_COL = "walkthrough_nuggets"       # cached (file, consumer) nuggets


# ---- Public: use from /walkthrough/next ----
async def stream_walkthrough_next(
    repo_id: str,
    entry_point: str | None = None,
    current_file_path: str | None = None,
    level: int | None = None,
):
    """
    Behavior:
      - If current_file_path is NOT provided: return the nugget for the FIRST file in the plan.
      - If current_file_path AND level are provided: find that step in the sequence,
        then return the nugget for the NEXT step (index + 1).
    """

    # Ensure async generator even on early return
    if False:
        yield ""

    mongo = get_mongo_client()
    mental_model = mongo[MENTAL_MODEL_COL]
    nuggets = mongo[WALK_NUGGETS_COL]
    neo4j = get_neo4j_client()

    # 1) Load plans for this repo
    plan_docs = list(
        mental_model.find(
            {"repo_id": repo_id, "plan": {"$exists": True}},
            {"_id": 0, "entry_point": 1, "plan": 1},
        )
    )
    if not plan_docs:
        yield f"❌ No plan found for repo {repo_id}.\n"
        return

    # 2) Choose plan (match entry_point if provided, else first)
    if entry_point:
        chosen = next((d for d in plan_docs if d.get("entry_point") == entry_point), None)
        if not chosen:
            chosen = plan_docs[0]
    else:
        chosen = plan_docs[0]

    plan = chosen.get("plan") or {}
    sequence = plan.get("sequence") or []  # list of {file_path, parent_file, level}
    if not sequence:
        yield "✅ Walkthrough complete.\n"
        return

    # 3) Determine which index to fetch
    if not current_file_path:
        # No file provided → always return FIRST file’s nugget
        target_idx = 0
    else:
        # Must have BOTH current_file_path and level to jump
        if level is None:
            yield "❌ You provided current_file_path without level. Both are required to jump.\n"
            return

        match_idx = None
        for i, step in enumerate(sequence):
            if step.get("file_path") == current_file_path and int(step.get("level", -1)) == int(level):
                match_idx = i
                break

        if match_idx is None:
            yield f"❌ ({current_file_path}, level={level}) not found in plan.\n"
            return

        target_idx = match_idx + 1  # fetch next in sequence

    # 4) Bounds check
    if target_idx >= len(sequence):
        yield "✅ Walkthrough complete.\n"
        return

    step = sequence[target_idx]
    file_path = step.get("file_path")
    consumer_path = step.get("parent_file")
    lvl = int(step.get("level", 0))

    # 5) Serve cached nugget if present
    cached = nuggets.find_one(
        {"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path}
    )
    if cached and "summary" in cached:
        yield f"\n## {file_path}\n"
        yield cached["summary"] + "\n"
        return

    # 6) Otherwise, generate a fresh nugget (then cache it)
    repo_summary = get_repo_summary(mongo, repo_id) or ""
    file_code = fetch_code_file(file_path=file_path) or ""
    try:
        upstream_files, downstream_files = neo4j.file_dependencies_v2(
            repo_id=repo_id, file_path=file_path
        )
    except Exception:
        upstream_files, downstream_files = [], []

    system_prompt = (
        "You are generating a plain-English walkthrough of a code repository.\n"
        "Explain the CURRENT FILE and, if a consumer is given, how this file serves that consumer.\n"
        "Guidelines:\n"
        "- Explain the code clearly and concisely.\n"
        "- Cater to someone who is unfamiliar with the codebase.\n"
        "- If this is an entry point (no consumer), explain what it starts/boots and what it orchestrates.\n"
        "- Use dependency lists as context; do not enumerate every import."
    )
    user_prompt = (
        f"REPO_ID: {repo_id}\n"
        f"ENTRY_POINT: {chosen.get('entry_point')}\n"
        f"LEVEL: {lvl}\n"
        f"CONSUMER_FILE: {consumer_path or '(none – entry point)'}\n"
        f"CURRENT_FILE: {file_path}\n\n"
        f"REPO_OVERVIEW:\n{repo_summary}\n\n"
        f"UPSTREAM_DEPENDENCIES:\n{upstream_files}\n\n"
        f"DOWNSTREAM_DEPENDENCIES:\n{downstream_files}\n\n"
        f"CURRENT_FILE_CODE:\n<<<\n{file_code}\n>>>\n\n"
        "Write the nugget now."
    )

    captured = []
    llm_stream = GroqLLM().generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=0.0,
        stream=True,
    )
    for ch in llm_stream:
        content = ch.choices[0].delta.content or ""
        if content:
            captured.append(content)
            yield content
    yield "\n"

    nugget_text = "".join(captured).strip()
    nuggets.update_one(
        {"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path},
        {"$set": {
            "repo_id": repo_id,
            "file_path": file_path,
            "consumer_path": consumer_path,
            "summary": nugget_text
        }},
        upsert=True,
    )

# ---- Helpers ----


def clear_repo_walkthrough_sessions(repo_id: str) -> int:
    """
    Remove all walkthrough sessions for this repo so a new run starts clean.
    Returns the number of deleted session documents.
    """
    mongo = get_mongo_client()
    sessions = mongo[WALK_SESSIONS_COL]
    res = sessions.delete_many({"repo_id": repo_id})
    return getattr(res, "deleted_count", 0)


# ---- Walkthrough method --

async def build_repo_walkthrough_plan(
    repo_id: str,
    depth: int = 3,
) -> dict:
    """
    Build deterministic walkthrough **plans** for ALL entry points in a repo.

    For each entry point, we BFS over **downstream** file dependencies (via Neo4j)
    up to `depth`. Unlike the earlier single-EP version, the `sequence` now captures
    EVERY traversal event (parent -> child), so a file that is reached from multiple
    parents will appear **multiple times** in the sequence with different parents.

    Returns JSON-serializable dict:
    {
      "repo_id": ...,
      "depth": int,
      "entry_points": ["ep1", "ep2", ...],
      "plans": [
        {
          "entry_point": "ep1",
          "nodes": [ { "file_path": "...", "level": int }, ... ],           # unique nodes with min-level
          "edges": [ { "from": "parent_fp or null", "to": "child_fp" }, ... ],
          "sequence": [ { "file_path": "...", "parent_file": null|"...", "level": int }, ... ]  # includes duplicates for multi-parents
        },
        ...
      ]
    }
    """
    mongo = get_mongo_client()
    neo = get_neo4j_client()

    entry_points = get_entry_point_files(mongo, repo_id) or []
    entry_points = [ep for ep in entry_points if isinstance(ep, str) and ep.strip()]
    if not entry_points:
        return {
            "repo_id": repo_id,
            "depth": depth,
            "entry_points": [],
            "plans": [],
            "error": "No entry points found for this repo."
        }

    def _plan_for_entry(entry_fp: str) -> dict:
        # Per-EP BFS with:
        # - `expanded`: files whose children we've already fetched (avoid re-expansion loops)
        # - `nodes_out`: unique nodes with MIN level encountered
        # - `edges_out`: all edges encountered (may contain duplicates if discovered via different paths; we dedupe)
        # - `sequence_out`: EVERY traversal event including multiple appearances for same file under different parents

        # print(f"Building walkthrough plan for repo {repo_id} from entry point {entry_fp} with depth {depth}")
        expanded: Set[str] = set()
        nodes_out: Dict[str, dict] = {}
        edges_out: List[dict] = []
        edges_seen: Set[tuple] = set()
        sequence_out: List[dict] = []

        # Queue holds traversal events (file_path, parent_file, level)
        from collections import deque
        q = deque()
        q.append((entry_fp, None, 0))

        # Initialize nodes/sequence for root
        if entry_fp not in nodes_out:
            nodes_out[entry_fp] = {"file_path": entry_fp, "level": 0}
        sequence_out.append({"file_path": entry_fp, "parent_file": None, "level": 0})

        while q:
            cur, parent, level = q.popleft()
            # Respect depth limit for EXPANSION (we still record the event itself above)
            if level >= depth:
                # Do not expand children beyond depth
                continue

            # Expand this node once
            if cur in expanded:
                continue

            expanded.add(cur)

            # Fetch downstream deps (files this file references)
            try:
                _up, downstream = neo.file_dependencies_v2(cur, repo_id)
            except Exception as e:
                print(f"EP {entry_fp} - failed to fetch downstream for {cur}: {e}")
                downstream = []

            # Deterministic order
            children = sorted(set(downstream))

            for child in children:
                child_level = level + 1

                # Update nodes_out with MIN level seen
                prev = nodes_out.get(child)
                if prev is None or child_level < prev.get("level", 10**9):
                    nodes_out[child] = {"file_path": child, "level": child_level}

                # Record edge (dedup)
                edge_key = (cur, child)
                if edge_key not in edges_seen:
                    edges_seen.add(edge_key)
                    edges_out.append({"from": cur, "to": child})

                # Record this traversal event in SEQUENCE (even if child seen before via other parents)
                sequence_out.append({"file_path": child, "parent_file": cur, "level": child_level})

                # Enqueue for potential expansion later
                q.append((child, cur, child_level))

        # Return plan for this EP
        plan = {
            "entry_point": entry_fp,
            "nodes": sorted(nodes_out.values(), key=lambda n: (n["level"], n["file_path"])),
            "edges": edges_out,
            "sequence": sequence_out,
        }

        # Add the plan for this entry point and repo in the DB for future reference
        mental_model = mongo[MENTAL_MODEL_COL]
        mental_model.update_one(
            {"repo_id": repo_id, "entry_point": entry_fp},
            {"$set": {
                "repo_id": repo_id,
                "entry_point": entry_fp,
                "plan": plan,
            }},
            upsert=True
        )

        return plan

    plans = [_plan_for_entry(ep) for ep in entry_points]

    return {
        "repo_id": repo_id,
        "depth": depth,
        "entry_points": entry_points,
        "plans": plans,
    }


async def stream_walkthrough_goto(repo_id: str, file_path: str):
    """
    Simplified version:
    Stream only the nugget (summary) for a given file_path.
    Does NOT modify traversal events, mark visited, or expand dependencies.
    """
    # Ensure this is compiled as an async generator
    if False:
        yield ""

    from .db import get_mongo_client, get_repo_summary, get_neo4j_client
    from .llm import GroqLLM
    from .tools import fetch_code_file

    mongo = get_mongo_client()
    nuggets = mongo[WALK_NUGGETS_COL]
    neo4j = get_neo4j_client()

    # --- Context prep ---
    repo_summary = get_repo_summary(mongo, repo_id) or ""
    file_code = fetch_code_file(file_path=file_path) or ""

    try:
        upstream_files, downstream_files = neo4j.file_dependencies_v2(repo_id=repo_id, file_path=file_path)
    except Exception:
        upstream_files, downstream_files = [], []

    # --- Header ---
    yield f"### {file_path}\n\n"

    # --- Serve cached nugget if available ---
    cached = nuggets.find_one({"repo_id": repo_id, "file_path": file_path})
    if cached and "summary" in cached:
        yield cached["summary"] + "\n"
        return

    # --- Otherwise, generate new nugget via LLM ---
    llm = GroqLLM()
    system_prompt = (
        "You are generating a plain-English walkthrough of a code repository.\n"
        "Explain the CURRENT FILE and, if a consumer is given, how this file serves that consumer.\n"
        "Guidelines:\n"
        "- Explain the code clearly and concisely.\n"
        "- Cater to someone who is unfamiliar with the codebase.\n"
        "- If this is an entry point (no consumer), explain what it starts/boots and what it orchestrates.\n"
        "- Use dependency lists as context; do not enumerate every import."
    )

    user_prompt = (
        f"REPO_ID: {repo_id}\n"
        f"FILE_PATH: {file_path}\n\n"
        f"REPO_OVERVIEW:\n{repo_summary}\n\n"
        f"UPSTREAM_DEPENDENCIES:\n{upstream_files}\n\n"
        f"DOWNSTREAM_DEPENDENCIES:\n{downstream_files}\n\n"
        f"CODE:\n<<<\n{file_code}\n>>>\n\n"
        "Write the explanation now."
    )

    captured = []
    qa_answer = llm.generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=0.0,
        stream=True,
    )

    for chunk in qa_answer:
        content = chunk.choices[0].delta.content or ""
        if content:
            captured.append(content)
            yield content
    yield "\n"

    nugget_text = "".join(captured).strip()

    # --- Cache nugget for reuse ---
    nuggets.update_one(
        {"repo_id": repo_id, "file_path": file_path},
        {"$set": {
            "repo_id": repo_id,
            "file_path": file_path,
            "summary": nugget_text,
        }},
        upsert=True
    )
