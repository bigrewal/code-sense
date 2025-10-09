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
    Plan-driven NEXT:
      - Reads the precomputed plan from mental_model (one doc per entry_point).
      - Tracks a per-entry-point cursor in session (plan_cursors).
      - If current_file_path/level are given, repositions the cursor to that step.
      - Streams the nugget for the step at the cursor, then advances the cursor by 1.
    Fallback:
      - If no plan is found, falls back to event-based traversal (existing behavior).
    """
    # Ensure async generator even if we early-return
    if False:
        yield ""

    mongo = get_mongo_client()
    sessions = mongo[WALK_SESSIONS_COL]
    nuggets = mongo[WALK_NUGGETS_COL]
    neo4j = get_neo4j_client()

    mental_model = mongo[MENTAL_MODEL_COL]

    # --- 1) Try to load plan(s) for this repo ---
    plan_docs = list(mental_model.find(
        {"repo_id": repo_id, "plan": {"$exists": True}},
        {"_id": 0, "entry_point": 1, "plan": 1}
    ))

    if not plan_docs:
        # ---- Fallback to your previous event-based traversal ----
        # (exactly your prior logic)
        session = _get_or_create_session(mongo, repo_id)
        _seed_events_if_empty(session, sessions, repo_id)

        events = session.get("traversal_events", [])
        idx = int(session.get("cursor", 0))

        # skip already-visited events
        visited_events = set(tuple(x) for x in session.get("visited_events", []))
        while idx < len(events):
            ev_key = (events[idx].get("file"), events[idx].get("parent"))
            if ev_key in visited_events:
                idx += 1
            else:
                break
        if idx != session.get("cursor", 0):
            session["cursor"] = idx
            sessions.update_one({"_id": session["_id"]}, {"$set": {"cursor": idx}})

        if not events or idx >= len(events):
            yield "✅ Walkthrough complete.\n"
            sessions.update_one({"_id": session["_id"]}, {"$set": {"done": True}})
            return

        ev = events[idx]
        file_path = ev["file"]
        consumer_path = ev["parent"]
        lvl = ev.get("level", 0)

        # cache first?
        cached = nuggets.find_one({"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path})
        if cached and "summary" in cached:
            yield f"\n## {file_path}\n"
            yield cached["summary"] + "\n"
            _extend_events_with_downstream_once(neo4j, repo_id, ev, session)
            _mark_event_visited_and_advance(sessions, session, ev, event_idx=idx)
            return

        # build + stream nugget
        repo_summary = get_repo_summary(mongo, repo_id) or ""
        file_code = fetch_code_file(file_path=file_path) or ""
        upstream_files, downstream_files = neo4j.file_dependencies(repo_id=repo_id, file_path=file_path)

        llm = GroqLLM()
        system_prompt = (
            "You are generating a concise, plain-English walkthrough of a code repository.\n"
            "Explain the CURRENT FILE and, if a consumer is given, how this file serves that consumer.\n"
            "Guidelines:\n"
            "- 4–8 lines. No code blocks. Avoid jargon; be concrete.\n"
            "- If this is an entry point (no consumer), explain what it starts/boots and what it orchestrates.\n"
            "- Use dependency lists as context; do not enumerate every import."
        )
        user_prompt = (
            f"REPO_ID: {repo_id}\n"
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
        llm_stream = GroqLLM().generate(prompt=user_prompt, system_prompt=system_prompt, temperature=0.0, stream=True)
        for ch in llm_stream:
            content = ch.choices[0].delta.content or ""
            if content:
                captured.append(content)
                yield content
        yield "\n"
        nugget_text = "".join(captured).strip()

        # cache
        nuggets.update_one(
            {"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path},
            {"$set": {"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path, "summary": nugget_text}},
            upsert=True,
        )

        _extend_events_with_downstream_once(neo4j, repo_id, ev, session)
        _mark_event_visited_and_advance(sessions, session, ev, event_idx=idx)
        return

    # --- 2) We have plans: pick the one to use ---
    if entry_point:
        chosen = next((d for d in plan_docs if d.get("entry_point") == entry_point), None)
        if not chosen:
            # fall back to first available plan
            chosen = plan_docs[0]
    else:
        chosen = plan_docs[0]

    ep = chosen["entry_point"]
    plan = chosen["plan"] or {}
    sequence = plan.get("sequence") or []  # list of {file_path, parent_file, level}

    # --- 3) Track per-entry-point cursor in session ---
    session = _get_or_create_session(mongo, repo_id)
    cursors = session.get("plan_cursors", {})  # {entry_point: idx}
    idx = int(cursors.get(ep, 0))

    # If UI gave us a current_file_path (and optional level), reposition cursor to that step
    if current_file_path:
        # Find the first matching sequence position
        match_idx = None
        if level is not None:
            for i, ev in enumerate(sequence):
                if ev.get("file_path") == current_file_path and int(ev.get("level", -1)) == int(level):
                    match_idx = i
                    break
        else:
            for i, ev in enumerate(sequence):
                if ev.get("file_path") == current_file_path:
                    match_idx = i
                    break
        if match_idx is not None:
            idx = match_idx
            cursors[ep] = idx
            sessions.update_one({"_id": session["_id"]}, {"$set": {"plan_cursors": cursors}})
        # else: keep existing idx

    # End if sequence exhausted
    if not sequence or idx >= len(sequence):
        yield "✅ Walkthrough complete.\n"
        # mark done for this EP (optional: set a map of done flags)
        sessions.update_one({"_id": session["_id"]}, {"$set": {"done": True}})
        return

    step = sequence[idx]
    file_path = step.get("file_path")
    consumer_path = step.get("parent_file")
    lvl = int(step.get("level", 0))

    # --- 4) Stream nugget (from cache if available) ---
    cached = nuggets.find_one({"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path})
    if cached and "summary" in cached:
        yield f"\n## {file_path}\n"
        yield cached["summary"] + "\n"
        # advance plan cursor by 1 for this EP
        cursors[ep] = idx + 1
        sessions.update_one({"_id": session["_id"]}, {"$set": {"plan_cursors": cursors}})
        return

    # Build fresh nugget from code + deps
    repo_summary = get_repo_summary(mongo, repo_id) or ""
    file_code = fetch_code_file(file_path=file_path) or ""
    try:
        upstream_files, downstream_files = neo4j.file_dependencies(repo_id=repo_id, file_path=file_path)
    except Exception:
        upstream_files, downstream_files = [], []

    system_prompt = (
        "You are generating a concise, plain-English walkthrough of a code repository.\n"
        "Explain the CURRENT FILE and, if a consumer is given, how this file serves that consumer.\n"
        "Guidelines:\n"
        "- 4–8 lines. No code blocks. Avoid jargon; be concrete.\n"
        "- If this is an entry point (no consumer), explain what it starts/boots and what it orchestrates.\n"
        "- Use dependency lists as context; do not enumerate every import."
    )
    user_prompt = (
        f"REPO_ID: {repo_id}\n"
        f"ENTRY_POINT: {ep}\n"
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
    llm_stream = GroqLLM().generate(prompt=user_prompt, system_prompt=system_prompt, temperature=0.0, stream=True)
    for ch in llm_stream:
        content = ch.choices[0].delta.content or ""
        if content:
            captured.append(content)
            yield content
    yield "\n"
    nugget_text = "".join(captured).strip()

    # Cache
    nuggets.update_one(
        {"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path},
        {"$set": {"repo_id": repo_id, "file_path": file_path, "consumer_path": consumer_path, "summary": nugget_text}},
        upsert=True,
    )

    # Advance plan cursor for this entry point
    cursors[ep] = idx + 1
    sessions.update_one({"_id": session["_id"]}, {"$set": {"plan_cursors": cursors}})



def _extend_events_with_downstream_once(neo4j: Neo4jClient, repo_id: str, current_event: dict, session: dict) -> None:
    """
    Extend traversal_events with downstream children of current_event.file as NEW EVENTS
    (child, parent=current_file, level+1). Expand each FILE only once (expanded_files).
    Allow the same CHILD to appear under multiple parents (multi-parent sequence).
    """
    file_path = current_event["file"]
    level = current_event.get("level", 0)

    expanded_files = set(session.get("expanded_files", []))
    if file_path in expanded_files:
        return  # already expanded this file's children

    try:
        _up, downstream = neo4j.file_dependencies(repo_id, file_path)
    except Exception:
        downstream = []

    downstream = sorted(set(downstream))
    events = session.get("traversal_events", [])
    in_queue = {(e["file"], e["parent"], e.get("level", 0)) for e in events}

    # Append each child as its own event; allow duplicates across different parents,
    # but avoid exact-duplicate events (same child, same parent, same level).
    for child in downstream:
        ev = (child, file_path, level + 1)
        if ev not in in_queue:
            events.append({"file": child, "parent": file_path, "level": level + 1})

    # Mark this file as expanded
    expanded_files.add(file_path)
    session["expanded_files"] = list(expanded_files)
    session["traversal_events"] = events


def _mark_event_visited_and_advance(sessions_col, session_doc: dict, event: dict, event_idx: int | None = None) -> None:
    visited_events = set(tuple(x) for x in session_doc.get("visited_events", []))
    key = (event["file"], event["parent"])
    visited_events.add(key)
    session_doc["visited_events"] = [list(x) for x in visited_events]

    if event_idx is None:
        session_doc["cursor"] = int(session_doc.get("cursor", 0)) + 1
    else:
        session_doc["cursor"] = event_idx + 1

    if session_doc["cursor"] >= len(session_doc.get("traversal_events", [])):
        session_doc["done"] = True
    sessions_col.update_one({"_id": session_doc["_id"]}, {"$set": session_doc})

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
        # event-based fields
        "traversal_events": [],   # list of {file, parent, level}
        "visited_events": [],     # list of [file, parent]
        "expanded_files": [],     # list of files expanded once for children
        "cursor": 0,
        "done": False,
    }
    sessions.insert_one(doc)
    return doc


def clear_repo_walkthrough_sessions(repo_id: str) -> int:
    """
    Remove all walkthrough sessions for this repo so a new run starts clean.
    Returns the number of deleted session documents.
    """
    mongo = get_mongo_client()
    sessions = mongo[WALK_SESSIONS_COL]
    res = sessions.delete_many({"repo_id": repo_id})
    return getattr(res, "deleted_count", 0)


def _seed_events_if_empty(session: dict, sessions_col, repo_id: str):
    if session.get("traversal_events"):
        return

    entry_points = session.get("entry_points") or []
    if not entry_points:
        # Optional: fallback to first overview file if you like
        # ep = _pick_any_file_as_entry(get_mongo_client()[MENTAL_MODEL_COL], repo_id)
        # entry_points = [ep] if ep else []
        pass

    events = [{"file": ep, "parent": None, "level": 0} for ep in entry_points if ep]
    session["traversal_events"] = events
    session["visited_events"] = []
    session["expanded_files"] = []
    session["cursor"] = 0
    session["done"] = False
    sessions_col.update_one({"_id": session["_id"]}, {"$set": session})


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
                _up, downstream = neo.file_dependencies(repo_id, cur)
            except Exception:
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
        upstream_files, downstream_files = neo4j.file_dependencies(repo_id=repo_id, file_path=file_path)
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
        "You are generating a concise, plain-English explanation of a code file.\n"
        "Explain what this file does, how it fits into the repository, and what responsibilities it has.\n"
        "Guidelines:\n"
        "- 4–8 lines. No code blocks. Avoid jargon.\n"
        "- Mention its role and its relationship to other modules if clear."
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
