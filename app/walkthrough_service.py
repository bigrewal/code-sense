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
async def stream_walkthrough_next(repo_id: str):
    """
    Advances the walkthrough by one step and streams a concise nugget.
    - Auto-selects an entry point for a new session.
    - Uses Neo4j to follow dependencies (downstream) deterministically.
    - Caches nuggets to avoid re-generation.

    FastAPI wiring example:
        @app.post("/walkthrough/next")
        async def walkthrough_next(req: WalkthroughRequest):
            return StreamingResponse(
                walkthrough_service.stream_walkthrough_next(req.repo_id),
                media_type="text/plain"
            )
    """
    mongo = get_mongo_client()
    sessions = mongo[WALK_SESSIONS_COL]
    nuggets = mongo[WALK_NUGGETS_COL]
    mental_model = mongo[MENTAL_MODEL_COL]
    neo4j = get_neo4j_client()

    # 1) Ensure session exists (auto-pick EP)
    session = _get_or_create_session(mongo, repo_id)

    if not session["traversal_queue"]:
        ep = session["entry_points"][0] if session["entry_points"] else _pick_any_file_as_entry(mental_model, repo_id)
        if not ep:
            yield "No files available to start a walkthrough for this repo.\n"
            return
        session["selected_entry_point"] = ep
        session["traversal_queue"] = [ep]
        session["parents"] = {ep: None}
        session["cursor"] = 0
        sessions.update_one({"_id": session["_id"]}, {"$set": session})

    # 2) Determine current step
    idx = session.get("cursor", 0)
    if idx >= len(session["traversal_queue"]):
        yield "✅ Walkthrough complete.\n"
        sessions.update_one({"_id": session["_id"]}, {"$set": {"done": True}})
        return

    file_path = session["traversal_queue"][idx]
    consumer_path = session["parents"].get(file_path)
    visited: Set[str] = set(session.get("visited", []))

    # 3) Try cached nugget first
    cached = nuggets.find_one({
        "repo_id": repo_id,
        "file_path": file_path,
        "consumer_path": consumer_path
    })
    if cached and "summary" in cached:
        yield _nugget_header(file_path, consumer_path)
        yield cached["summary"] + "\n"
        # Expand queue (if not expanded yet) from Neo4j downstream to keep traversal moving
        _extend_queue_with_downstream(neo4j, repo_id, file_path, session)
        _mark_visited_and_advance(sessions, session, file_path)
        return

    # 4) Build LLM inputs
    repo_summary = get_repo_summary(mongo, repo_id) or ""
    file_code = fetch_code_file(file_path=file_path) or ""

    # Query Neo4j for dependencies
    upstream_files, downstream_files = neo4j.file_dependencies(repo_id=repo_id, file_path=file_path)

    # 5) Generate nugget (streaming)
    yield _nugget_header(file_path, consumer_path)

    llm = GroqLLM()
    system_prompt = (
        "You are generating a concise, plain-English walkthrough of a code repository.\n"
        "Your task now: explain the CURRENT FILE and, if it has a consumer, how it serves that consumer.\n"
        "Guidelines:\n"
        "- No code blocks. Avoid jargon; be concrete.\n"
        "- If this is an entry point (no consumer), explain what it starts/boots and what it orchestrates.\n"
        "- Use the provided dependency lists only as context; do not list every import—focus on what matters."
    )

    user_prompt = (
        f"REPO_ID: {repo_id}\n\n"
        f"REPO_OVERVIEW:\n{repo_summary}\n\n"
        f"CONSUMER_FILE: {consumer_path or '(none – entry point)'}\n"
        f"CURRENT_FILE: {file_path}\n"
        f"UPSTREAM_DEPENDENCIES (files that reference CURRENT_FILE):\n{upstream_files}\n\n"
        f"DOWNSTREAM_DEPENDENCIES (files CURRENT_FILE references):\n{downstream_files}\n\n"
        f"CURRENT_FILE_CODE:\n<<<\n{file_code}\n>>>\n\n"
        "Write the nugget now."
    )

    # Stream the nugget and also capture it to persist
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

    # 6) Cache nugget
    nuggets.update_one(
        {"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path},
        {"$set": {
            "repo_id": repo_id,
            "file_path": file_path,
            "consumer_path": consumer_path,
            "summary": nugget_text,
        }},
        upsert=True
    )

    # 7) Extend traversal with Neo4j downstream deps (deterministic, no cycles)
    _extend_queue_with_downstream(neo4j, repo_id, file_path, session)

    # 8) Mark visited & advance cursor
    _mark_visited_and_advance(sessions, session, file_path)


# ---- Helpers ----

def _get_or_create_session(mongo, repo_id: str) -> Dict[str, Any]:
    sessions = mongo[WALK_SESSIONS_COL]
    existing = sessions.find_one({"repo_id": repo_id, "done": {"$ne": True}})
    if existing:
        return existing

    entry_points = get_entry_point_files(mongo, repo_id) or []
    doc = {
        "_id": str(uuid.uuid4()),
        "repo_id": repo_id,
        "entry_points": entry_points,
        "selected_entry_point": None,
        "traversal_queue": [],
        "parents": {},          # child -> parent (consumer)
        "visited": [],
        "cursor": 0,
        "done": False,
    }
    sessions.insert_one(doc)
    return doc


def _pick_any_file_as_entry(mental_model, repo_id: str) -> Optional[str]:
    doc = mental_model.find_one(
        {"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW"},
        {"file_path": 1},
        sort=[("_id", 1)]
    )
    return doc["file_path"] if doc else None


def _extend_queue_with_downstream(neo4j: Neo4jClient, repo_id: str, file_path: str, session: Dict[str, Any]) -> None:
    """
    Add downstream dependencies of `file_path` to the traversal queue in a stable (alphabetical) order.
    Set their parent consumer to `file_path`. Avoid duplicates and cycles.
    """
    _, downstream = neo4j.file_dependencies(repo_id=repo_id, file_path=file_path)

    queue = session["traversal_queue"]
    parents = session["parents"]
    visited = set(session.get("visited", []))
    in_queue = set(queue)

    # deterministic order
    downstream_sorted = sorted(downstream)

    for child in downstream_sorted:
        if child not in visited and child not in in_queue:
            queue.append(child)
            parents[child] = file_path
            in_queue.add(child)

    session["traversal_queue"] = queue
    session["parents"] = parents


def _mark_visited_and_advance(sessions_col, session_doc: Dict[str, Any], file_path: str) -> None:
    visited = set(session_doc.get("visited", []))
    visited.add(file_path)
    session_doc["visited"] = list(visited)
    session_doc["cursor"] = session_doc.get("cursor", 0) + 1
    # Complete if cursor reached tail
    if session_doc["cursor"] >= len(session_doc["traversal_queue"]):
        session_doc["done"] = True
    sessions_col.update_one({"_id": session_doc["_id"]}, {"$set": session_doc})


def _nugget_header(file_path: str, consumer_path: Optional[str]) -> str:
    if consumer_path:
        return f"\n## How {file_path} serves {consumer_path}\n"
    return f"\n## Entry point: {file_path}\n"


async def build_repo_walkthrough_plan(
    repo_id: str,
    depth: int = 3,
) -> dict:
    """
    Build a deterministic plan for a repo walkthrough:
    - Start from the first entry point (auto-picked from DB).
    - Traverse downstream dependencies via Neo4j (BFS).
    - Limit traversal depth with `depth`.
    
    Returns JSON-serializable dict with nodes, edges, and sequence.
    """
    mongo = get_mongo_client()
    neo = get_neo4j_client()

    entry_points = get_entry_point_files(mongo, repo_id) or []
    if not entry_points:
        return {
            "repo_id": repo_id,
            "depth": depth,
            "error": "No entry points found for this repo."
        }

    start_fp = entry_points[0]

    visited: set[str] = set()
    levels: dict[str, int] = {start_fp: 0}
    parents: dict[str, Optional[str]] = {start_fp: None}
    queue: list[str] = [start_fp]

    nodes_out: dict[str, dict] = {}
    edges_out: list[dict] = []
    sequence_out: list[dict] = []

    def _emit_node(fp: str):
        if fp not in nodes_out:
            nodes_out[fp] = {
                "file_path": fp,
                "level": levels.get(fp, 0),
            }

    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)

        _emit_node(cur)
        sequence_out.append({
            "file_path": cur,
            "parent_file": parents.get(cur),
            "level": levels.get(cur, 0),
        })

        cur_level = levels.get(cur, 0)
        if cur_level >= depth:
            continue

        # Downstream dependencies (files this file references)
        _, downstream = neo.file_dependencies(repo_id, cur)
        downstream_sorted = sorted(downstream)

        for child in downstream_sorted:
            if child not in visited and child not in queue:
                parents[child] = cur
                levels[child] = cur_level + 1
                queue.append(child)
                _emit_node(child)
                edges_out.append({"from": cur, "to": child})

    return {
        "repo_id": repo_id,
        "depth": depth,
        "entry_point": start_fp,
        "nodes": list(nodes_out.values()),
        "edges": edges_out,
        "sequence": sequence_out,
    }
