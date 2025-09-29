# walkthrough_def_service.py

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .db import Neo4jClient, get_mongo_client, get_neo4j_client
from .llm import GroqLLM
from .tools import fetch_code_file


# ---- Mongo collection names ----
DEF_WALK_SESSIONS_COL = "walkthrough_def_sessions"     # per-definition walkthrough sessions
DEF_WALK_NUGGETS_COL  = "walkthrough_def_nuggets"      # cached (node_id, parent_node_id)->nugget


# ---- Public API: call this from your FastAPI route ----
async def stream_definition_walkthrough(
    repo_id: str,
    file_path: str,
    definition_name: str,
    depth: int = 2,
):
    """
    Start (or continue) a definition-level walkthrough, streaming one 'step' (nugget) per call.
    - On first call, resolves the starting definition node and seeds the traversal.
    - Each call advances one definition in BFS order (downstream references), up to `depth`.
    - Streams a 4–8 line explanation ('nugget') for the current definition in context of its parent/caller.

    Example FastAPI wiring:

        from fastapi.responses import StreamingResponse
        from .walkthrough_def_service import stream_definition_walkthrough

        @app.post("/walkthrough/def/start")
        async def walkthrough_def_start(req: DefWalkRequest):
            return StreamingResponse(
                stream_definition_walkthrough(
                    repo_id=req.repo_id,
                    file_path=req.file_path,
                    definition_name=req.definition_name,
                    depth=req.depth or 2,
                ),
                media_type="text/plain"
            )
    """
    mongo = get_mongo_client()
    neo: Neo4jClient = get_neo4j_client()

    sessions = mongo[DEF_WALK_SESSIONS_COL]
    nuggets  = mongo[DEF_WALK_NUGGETS_COL]

    # 1) Find or create a session for this (repo_id, file_path, definition_name, depth)
    session = _get_or_create_def_session(
        sessions, neo, repo_id, file_path, definition_name, depth
    )
    if not session.get("queue"):
        yield f"Could not initialize walkthrough for {definition_name} in {file_path}.\n"
        return

    # 2) If finished
    idx = session.get("cursor", 0)
    if idx >= len(session["queue"]):
        yield "✅ Definition walkthrough complete.\n"
        sessions.update_one({"_id": session["_id"]}, {"$set": {"done": True}})
        return

    # 3) Current step
    current_id = session["queue"][idx]
    parent_id  = session["parents"].get(current_id)  # caller that led to this node (None for root)
    visited    = set(session.get("visited", []))

    # 4) Resolve node info and source snippet
    node = session["node_index"].get(current_id)
    if not node:
        yield f"Node {current_id} not found.\n"
        _advance_and_persist(sessions, session, current_id)
        return

    # Try cached nugget first
    cached = nuggets.find_one({
        "repo_id": repo_id,
        "node_id": current_id,
        "parent_node_id": parent_id,
    })
    if cached and "summary" in cached:
        # Header
        yield _nugget_header(node, session["symbol_index"], parent_id)
        yield cached["summary"] + "\n"
        # Extend queue (if not done earlier) and advance
        _extend_queue_bfs_level(neo, session, current_id)
        _advance_and_persist(sessions, session, current_id)
        return

    # Build snippet text from file + coordinates
    snippet_text = _extract_snippet_text(node["file_path"], node["start_line"], node["end_line"])

    # Neo4j neighbors for context
    neighbors_up = neo._neighbors_up(current_id, node["file_path"])
    neighbors_dn = neo._neighbors_down(current_id, node["file_path"])

    # Resolve names for neighbors (only those present in index)
    up_names = [_node_label(session, n) for n in neighbors_up if n in session["node_index"]]
    dn_names = [_node_label(session, n) for n in neighbors_dn if n in session["node_index"]]

    # Parent (caller) label
    parent_label = _node_label(session, parent_id) if parent_id else None

    # 5) Generate the nugget (stream)
    llm = GroqLLM()

    system_prompt = (
        "You are explaining a specific function or class in a codebase to a developer.\n"
        "Given the definition's source snippet and a bit of graph context:\n"
        " - Explain what the definition does and how it behaves.\n"
        " - If there is a caller/parent, explain briefly how this definition serves its parent.\n"
        " - Mention key params/returns and notable side effects ONLY if they are clear from the snippet.\n"
        " - 4–8 lines, no code blocks, no file-by-file lists. Be concise and concrete."
    )

    user_prompt = (
        f"REPO_ID: {repo_id}\n"
        f"DEFINITION: {node['file_path']} :: {session['symbol_index'].get(current_id, '')}\n"
        f"NODE_TYPE: {node['node_type']}\n"
        f"PARENT_CALLER: {parent_label or '(none – starting definition)'}\n\n"
        f"WHO_CALLS_ME (UPSTREAM NEIGHBORS): {up_names[:6]}\n"
        f"WHAT_I_CALL (DOWNSTREAM NEIGHBORS): {dn_names[:6]}\n\n"
        "SOURCE SNIPPET:\n"
        "<<<\n"
        f"{snippet_text}\n"
        ">>>\n\n"
        "Write the nugget now."
    )

    # Header first (so UI can render title promptly)
    yield _nugget_header(node, session["symbol_index"], parent_id)

    captured: List[str] = []
    stream = llm.generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=0.0,
        stream=True,
    )
    for chunk in stream:
        content = chunk.choices[0].delta.content or ""
        if content:
            captured.append(content)
            yield content
    yield "\n"

    nugget_text = "".join(captured).strip()

    # 6) Cache nugget
    nuggets.update_one(
        {"repo_id": repo_id, "node_id": current_id, "parent_node_id": parent_id},
        {"$set": {
            "repo_id": repo_id,
            "node_id": current_id,
            "parent_node_id": parent_id,
            "file_path": node["file_path"],
            "definition_name": session["symbol_index"].get(current_id, ""),
            "summary": nugget_text,
        }},
        upsert=True
    )

    # 7) Extend BFS queue with downstream neighbors (bounded by depth)
    _extend_queue_bfs_level(neo, session, current_id)

    # 8) Advance cursor & persist
    _advance_and_persist(sessions, session, current_id)


# ---- Session bootstrap & traversal helpers ----

def _get_or_create_def_session(
    sessions_col,
    neo,
    repo_id: str,
    file_path: str,
    definition_name: str,
    depth: int,
) -> Dict[str, Any]:
    # Try to reuse an open session for same key
    existing = sessions_col.find_one({
        "repo_id": repo_id,
        "file_path": file_path,
        "definition_name": definition_name,
        "depth": depth,
        "done": {"$ne": True},
    })
    if existing:
        return existing

    # Build node index for this repo to resolve names quickly
    def_nodes = neo._fetch_def_nodes(repo_id)
    node_index: Dict[str, Dict[str, Any]] = {}
    file_to_nodes: Dict[str, List[str]] = {}
    for n in def_nodes:
        node_index[n["node_id"]] = n
        file_to_nodes.setdefault(n["file_path"], []).append(n["node_id"])

    # Resolve the starting definition node id in this file by matching symbol name
    start_node_id = _resolve_definition_node_id(neo, node_index, file_to_nodes, file_path, definition_name)

    if not start_node_id:
        # If not found, create a minimal session that reports inability to start
        doc = {
            "_id": str(uuid.uuid4()),
            "repo_id": repo_id,
            "file_path": file_path,
            "definition_name": definition_name,
            "depth": depth,
            "queue": [],
            "parents": {},
            "visited": [],
            "cursor": 0,
            "done": True,
            "node_index": {},
            "symbol_index": {},
        }
        sessions_col.insert_one(doc)
        return doc

    # Build symbol index (node_id -> symbol name) once
    symbol_index: Dict[str, str] = {}
    for nid in node_index.keys():
        # only resolve for nodes in this file or start node’s immediate neighborhood later if you want to reduce calls;
        # for simplicity, resolve lazily for the start file first
        symbol_index[nid] = ""  # placeholder

    # minimally fill for start node + all nodes in the same file (cheap-ish)
    for nid in file_to_nodes.get(file_path, []):
        symbol_index[nid] = neo._fetch_symbol_from_ast(nid) or ""

    # BFS seed: start node at level 0
    doc = {
        "_id": str(uuid.uuid4()),
        "repo_id": repo_id,
        "file_path": file_path,
        "definition_name": definition_name,
        "depth": max(int(depth), 0),
        "queue": [start_node_id],
        "parents": {start_node_id: None},  # child -> parent (caller)
        "visited": [],
        "cursor": 0,
        "done": False,
        "node_index": node_index,
        "symbol_index": symbol_index,
        "levels": {start_node_id: 0},      # BFS depth per node
    }
    sessions_col.insert_one(doc)
    return doc


def _resolve_definition_node_id(
    neo,
    node_index: Dict[str, Dict[str, Any]],
    file_to_nodes: Dict[str, List[str]],
    file_path: str,
    definition_name: str,
) -> Optional[str]:
    """
    Among definitions in file_path, pick the one whose symbol matches `definition_name`.
    Falls back to first def in file if no exact match is found.
    """
    candidate_ids = file_to_nodes.get(file_path, [])
    # Try exact symbol match
    for nid in candidate_ids:
        name = neo._fetch_symbol_from_ast(nid) or ""
        if name:
            # fill back into symbol cache if present
            # (caller fills symbol_index; safe to ignore here)
            if name == definition_name:
                return nid

    # Fallback: case-insensitive match
    def_name_lower = (definition_name or "").lower()
    for nid in candidate_ids:
        name = neo._fetch_symbol_from_ast(nid) or ""
        if name.lower() == def_name_lower:
            return nid

    # Final fallback: first definition in file
    return candidate_ids[0] if candidate_ids else None


def _extend_queue_bfs_level(neo: Neo4jClient, session: Dict[str, Any], current_id: str) -> None:
    """
    BFS over downstream references (what this definition calls).
    Adds neighbors if:
      - Not visited, not already in queue
      - Its level would be <= session.depth
    Also records parent mapping (child -> current_id).
    """
    depth_limit = int(session.get("depth", 0))
    levels = session.get("levels", {})
    node_index = session["node_index"]

    current_level = levels.get(current_id, 0)
    next_level = current_level + 1
    if next_level > depth_limit:
        return

    # Get downstream neighbors
    node = node_index.get(current_id)
    if not node:
        return
    dn = neo._neighbors_down(current_id, node["file_path"])

    # Deterministic order; include only nodes we know about (in index)
    dn = [n for n in sorted(dn) if n in node_index]

    queue = session["queue"]
    in_queue = set(queue)
    visited = set(session.get("visited", []))
    parents = session["parents"]

    for child in dn:
        if child in visited or child in in_queue:
            continue
        queue.append(child)
        in_queue.add(child)
        parents[child] = current_id
        levels[child] = next_level
        # lazily resolve symbol for this child (optional optimization)
        if session["symbol_index"].get(child, "") == "":
            try:
                session["symbol_index"][child] = neo._fetch_symbol_from_ast(child) or ""
            except Exception:
                session["symbol_index"][child] = ""

    # persist back in session (caller persists doc)
    session["queue"] = queue
    session["parents"] = parents
    session["levels"] = levels


def _advance_and_persist(sessions_col, session_doc: Dict[str, Any], current_id: str) -> None:
    visited = set(session_doc.get("visited", []))
    visited.add(current_id)
    session_doc["visited"] = list(visited)
    session_doc["cursor"] = session_doc.get("cursor", 0) + 1
    if session_doc["cursor"] >= len(session_doc["queue"]):
        session_doc["done"] = True
    sessions_col.update_one({"_id": session_doc["_id"]}, {"$set": session_doc})


def _node_label(session: Dict[str, Any], node_id: Optional[str]) -> str:
    if not node_id:
        return ""
    n = session["node_index"].get(node_id)
    if not n:
        return node_id
    sym = session["symbol_index"].get(node_id) or ""
    if sym:
        return f"{sym} ({n['file_path']})"
    return f"{n['node_type']} @ {n['file_path']}:{n['start_line']}"


def _nugget_header(node: Dict[str, Any], symbol_index: Dict[str, str], parent_id: Optional[str]) -> str:
    name = symbol_index.get(node["node_id"], "") or "(anonymous)"
    if parent_id:
        return f"\n### How `{name}` serves its caller\n"
    return f"\n### Definition: `{name}`\n"


def _extract_snippet_text(file_path: str, start_line: int, end_line: int) -> str:
    """
    Pull the file and slice lines [start_line, end_line] inclusive.
    Falls back gracefully if indices are invalid.
    """
    code = fetch_code_file(file_path=file_path) or ""
    lines = code.splitlines()
    s = max(1, int(start_line or 1))
    e = max(s, int(end_line or s))
    # convert to 0-based
    s0 = max(0, s - 1)
    e0 = min(len(lines), e)
    if s0 >= len(lines):
        return ""
    snippet = "\n".join(lines[s0:e0])
    return snippet

async def build_definition_walkthrough_plan(
    repo_id: str,
    file_path: str,
    definition_name: str,
    depth: int = 2,
) -> dict:
    """
    Compute a deterministic, depth-bounded walkthrough **plan** for a given definition.
    The plan is a graph (nodes + edges) plus a linear BFS 'sequence' that a UI can render as:
      Def A --(depth=1)--> Def B --(depth=2)--> Def C

    Returns JSON-serializable dict:
    {
      "repo_id": ...,
      "file_path": ...,
      "definition_name": ...,
      "depth": 2,
      "start": { "node_id": ..., "symbol": ..., "file_path": ..., "node_type": ..., "level": 0 },
      "nodes": [ { "node_id": ..., "symbol": ..., "file_path": ..., "node_type": ..., "level": int }, ... ],
      "edges": [ { "from": <parent_node_id or null>, "to": <child_node_id> }, ... ],
      "sequence": [ { "node_id": ..., "parent_node_id": null|str, "level": int }, ... ]
    }
    """
    neo = get_neo4j_client()

    # 1) Build index of all definition nodes in the repo
    def_nodes = neo._fetch_def_nodes(repo_id)
    node_index: Dict[str, Dict[str, Any]] = {n["node_id"]: n for n in def_nodes}
    file_to_nodes: Dict[str, List[str]] = {}
    for n in def_nodes:
        file_to_nodes.setdefault(n["file_path"], []).append(n["node_id"])

    # 2) Resolve the start node for (file_path, definition_name)
    start_node_id = _resolve_definition_node_id(neo, node_index, file_to_nodes, file_path, definition_name)
    if not start_node_id:
        return {
            "repo_id": repo_id,
            "file_path": file_path,
            "definition_name": definition_name,
            "depth": int(depth),
            "error": f"Could not find a definition named '{definition_name}' in {file_path}.",
        }

    # 3) Symbol cache (resolve lazily)
    symbol_index: Dict[str, str] = {}

    def _sym(nid: str) -> str:
        s = symbol_index.get(nid)
        if s is None or s == "":
            try:
                symbol_index[nid] = neo._fetch_symbol_from_ast(nid) or ""
            except Exception:
                symbol_index[nid] = ""
        return symbol_index[nid]

    # Pre-resolve for the start node and all defs in the same file (helps ordering)
    for nid in file_to_nodes.get(file_path, []):
        _sym(nid)

    # 4) BFS over downstream neighbors, bounded by `depth`
    depth_limit = max(int(depth), 0)
    visited: set[str] = set()
    levels: Dict[str, int] = {start_node_id: 0}
    queue: List[str] = [start_node_id]
    parents: Dict[str, Optional[str]] = {start_node_id: None}

    # Graph accumulators
    nodes_out: Dict[str, Dict[str, Any]] = {}
    edges_out: List[Dict[str, Optional[str]]] = []
    sequence_out: List[Dict[str, Optional[str]]] = []

    def _emit_node(nid: str):
        if nid in nodes_out:
            return
        n = node_index.get(nid, {})
        nodes_out[nid] = {
            "node_id": nid,
            "symbol": _sym(nid),
            "file_path": n.get("file_path", ""),
            "node_type": n.get("node_type", ""),
            "level": levels.get(nid, 0),
        }

    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)

        # Emit current node + its sequence occurrence
        _emit_node(cur)
        sequence_out.append({
            "node_id": cur,
            "parent_node_id": parents.get(cur),
            "level": levels.get(cur, 0),
        })

        # Stop at depth limit
        cur_level = levels.get(cur, 0)
        if cur_level >= depth_limit:
            continue

        # Downstream neighbors (what this definition calls)
        cur_node = node_index.get(cur)
        if not cur_node:
            continue
        dn = neo._neighbors_down(cur, cur_node["file_path"])
        # Keep only known nodes
        dn = [nid for nid in dn if nid in node_index]

        # Deterministic ordering by (symbol.lower(), file_path, node_id)
        def sort_key(nid: str):
            return (_sym(nid).lower(), node_index[nid].get("file_path", ""), nid)

        for child in sorted(dn, key=sort_key):
            if child not in visited and child not in queue:
                parents[child] = cur
                levels[child] = cur_level + 1
                queue.append(child)
                _emit_node(child)
                edges_out.append({"from": cur, "to": child})

    # 5) Prepare final payload
    start_node = node_index[start_node_id]
    start_out = {
        "node_id": start_node_id,
        "symbol": _sym(start_node_id),
        "file_path": start_node.get("file_path", ""),
        "node_type": start_node.get("node_type", ""),
        "level": 0,
    }

    return {
        "repo_id": repo_id,
        "file_path": file_path,
        "definition_name": definition_name,
        "depth": depth_limit,
        "start": start_out,
        "nodes": list(nodes_out.values()),
        "edges": edges_out,
        "sequence": sequence_out,
    }

