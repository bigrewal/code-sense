from __future__ import annotations
"""
Planner-based retriever (v2, simplified, Mongo-first) with 2 intent classes:
  - definition_location
  - entry_point_files

Pipeline:
  1) Router (LLM) -> which classes to run (minimal set).
  2) Per-class selector (LLM over compact records) -> IDs only.
  3) Resolver (Mongo-backed): fetch full docs by ID; 1-hop expand via dependencies; dedupe.
  4) Ordering (human-first): Entry points → Definitions → Upstream → Downstream → Closure extras.

Notes:
  - No Neo4j, no synthesizer, no certificates, no look_for.
  - LLM I/O is STRICT JSON; selectors return only {"records_used":[ids...]}.
  - Modular: each class registers (records_provider, resolver).

Public entrypoint:
  retrieve_records_planner(question: str, repo_name: str, repo_overview: dict, llm, ep_records: list[dict] | None = None)
  -> {"files": [{"file_path": str}, ...]}
"""

from typing import List, Dict, Set, Tuple, Optional, Any
import json
import os

# --- Mongo client (adjust import to your project) ---
from .db import get_mongo_client
from .llm import GroqLLM
mongo_client = get_mongo_client()
mental_model_collection = mongo_client["mental_model"]

# ====================
# Router (LLM #1)
# ====================

ROUTER_SYSTEM_CORE = (
    "You are a planner that selects which retrieval classes to run for answering a repository question.\n"
    "Allowed classes: definition_location, entry_point_files.\n"
    "Always return the minimal sufficient set of classes.\n"
    "Return STRICT JSON: {\"classes\":[...], \"hints\":{class:[strings]}}.\n"
    "Output JSON only."
)

ROUTER_FEWSHOTS = [
    {
        "q": "Where is function X defined?",
        "json": "{\"classes\": [\"definition_location\"], \"hints\": {\"definition_location\": [\"X\"]}}",
        "classes": ["definition_location"],
    },
    {
        "q": "What are the entry point files in this repo?",
        "json": "{\"classes\": [\"entry_point_files\"], \"hints\": {\"entry_point_files\": []}}",
        "classes": ["entry_point_files"],
    },
    {
        "q": "How does it work?",
        "json": "{\"classes\": [\"entry_point_files\", \"definition_location\"], \"hints\": {\"entry_point_files\": [], \"definition_location\": []}}",
        "classes": ["entry_point_files", "definition_location"],
    },
]

def build_router_system(allowed_classes: List[str]) -> str:
    shots = []
    for ex in ROUTER_FEWSHOTS:
        if all(c in allowed_classes for c in ex["classes"]):
            shots.append(f"Q: {ex['q']}\n→ {ex['json']}")
    return ROUTER_SYSTEM_CORE + "\n\nFew-shot examples:\n" + "\n\n".join(shots[:6])

def build_router_prompt(repo_name: str, question: str, repo_overview: Dict[str, Any]) -> str:
    catalog = json.dumps(repo_overview, ensure_ascii=False)
    return (
        f"QUESTION:\n{question}\n\n"
        f"REPO OVERVIEW (compact catalog):\n{catalog}\n\n"
        "OUTPUT JSON SCHEMA strictly:\n"
        "{\n  \"classes\": [\"definition_location\", \"entry_point_files\"],\n"
        "  \"hints\": {\"definition_location\": [], \"entry_point_files\": []}\n}\n"
    )

# ====================
# Class selector (LLM over class-specific records) → IDs only
# ====================

CLASS_SYSTEM = (
    "You answer using ONLY the provided class-specific records.\n"
    "Return STRICT JSON: {\"records_used\":[ids]}\n"
)

def chunk_records(records: List[dict], max_per_chunk: int = 25) -> List[List[dict]]:
    if len(records) <= max_per_chunk:
        return [records]
    overlap = max(1, max_per_chunk // 10)  # 10% overlap to avoid boundary misses
    chunks = []
    i, n = 0, len(records)
    while i < n:
        j = min(n, i + max_per_chunk)
        chunk = records[i:j]
        if chunks and overlap > 0:
            prev_tail = records[max(0, i - overlap):i]
            chunk = prev_tail + chunk
        chunks.append(chunk)
        i = j
    return chunks

def exec_class_llm(question: str, class_name: str, records: List[dict], llm: GroqLLM, hints: Optional[List[str]] = None) -> Dict[str, Any]:
    if not records:
        return {"records_used": []}

    merged_ids: List[str] = []
    for chunk in chunk_records(records):
        payload = json.dumps({"class": class_name, "records": chunk}, ensure_ascii=False)
        prompt = (
            f"QUESTION:\n{question}\n\n"
            f"CLASS INPUT:\n{payload}\n\n"
            "Output JSON strictly as: {\"records_used\":[ids]}"
        )
        raw = llm.generate(prompt=prompt, system_prompt=CLASS_SYSTEM, temperature=0.0)
        data = safe_json(raw)
        if not isinstance(data, dict):
            continue
        for rid in data.get("records_used", []) or []:
            if isinstance(rid, str) and rid not in merged_ids:
                merged_ids.append(rid)
    return {"records_used": merged_ids}

def safe_json(raw: str) -> Any:
    try:
        start = raw.find('{'); end = raw.rfind('}')
        if start == -1 or end == -1 or end <= start:
            return {}
        return json.loads(raw[start:end+1])
    except Exception:
        return {}

# ====================
# Mongo helpers
# ====================

def get_definition_records_compact(repo_name: str) -> List[dict]:
    """Compact definition records for LLM selection (no deps)."""
    cur = mental_model_collection.find(
        {"repo_name": repo_name, "type": "DEFINITIONS_RECORD"},
        {"_id": 0, "id": 1, "name": 1, "signature": 1, "summary": 1}
    )
    return list(cur)

def fetch_defs_by_ids(ids: List[str]) -> List[dict]:
    if not ids:
        return []
    cur = mental_model_collection.find(
        {"id": {"$in": ids}, "type": "DEFINITIONS_RECORD"},
        {"_id": 0, "id": 1, "file_path": 1, "name": 1, "signature": 1, "location": 1,
         "summary": 1, "dependencies.upstream": 1, "dependencies.downstream": 1}
    )
    return list(cur)

def fetch_file_paths_by_ids(ids: List[str]) -> List[str]:
    if not ids:
        return []
    cur = mental_model_collection.find({"id": {"$in": ids}}, {"_id": 0, "file_path": 1})
    return [d["file_path"] for d in cur if d.get("file_path")]

# ====================
# Per-class providers & resolvers (modular)
# ====================

# --- definition_location ---
def records_provider_definition(repo_name: str) -> List[dict]:
    return get_definition_records_compact(repo_name)

def resolver_definition(selected_ids: List[str]) -> Dict[str, Set[str]]:
    zones = {"entrypoints": set(), "defs": set(), "upstream": set(), "downstream": set(), "closure": set()}
    docs = fetch_defs_by_ids(selected_ids)
    def_files = {d["file_path"] for d in docs if d.get("file_path")}
    zones["defs"] |= def_files

    # 1-hop via dependencies (cap fan-out)
    up_ids, dn_ids = set(), set()
    for d in docs:
        ups = ((d.get("dependencies") or {}).get("upstream") or [])[:5]
        dns = ((d.get("dependencies") or {}).get("downstream") or [])[:5]
        for u in ups:
            uid = u.get("id")
            if uid: up_ids.add(uid)
        for v in dns:
            vid = v.get("id")
            if vid: dn_ids.add(vid)

    if up_ids:
        zones["upstream"] |= set(fetch_file_paths_by_ids(list(up_ids)))
    if dn_ids:
        zones["downstream"] |= set(fetch_file_paths_by_ids(list(dn_ids)))

    return zones

# --- entry_point_files ---
def records_provider_entrypoints(repo_name: str) -> List[dict]:
    """
    EP records are supplied by caller for now.
    Each must have: id (ep:...), file_path, downstream_files (list[str]).
    """
    recs = []
    recs.append({"id": "data/dictquery/dictquery/__init__.py", "file_path": "data/dictquery/dictquery/__init__.py"})
    # for r in ep_records or []:
    #     if isinstance(r, dict) and r.get("id") and r.get("file_path"):
    #         # keep compact shape for LLM; it only needs id (and we pass file_path too for context)
    #         recs.append({"id": r["id"], "file_path": r["file_path"]})
    return recs

def resolver_entrypoints(selected_ids: List[str], ep_records: Optional[List[dict]]) -> Dict[str, Set[str]]:
    zones = {"entrypoints": set(), "defs": set(), "upstream": set(), "downstream": set(), "closure": set()}
    by_id = {r["id"]: r for r in (ep_records or []) if isinstance(r, dict) and r.get("id")}
    for rid in selected_ids:
        rec = by_id.get(rid)
        if not rec:
            continue
        fp = rec.get("file_path")
        if fp:
            zones["entrypoints"].add(fp)
        for d in (rec.get("downstream_files") or [])[:10]:
            zones["downstream"].add(d)
    return zones

# ====================
# Ordering (human-first)
# ====================

def is_packaging(path: str) -> bool:
    base = (path or "").rsplit("/", 1)[-1].lower()
    return base in {"setup.py", "pyproject.toml", "poetry.lock", "requirements.txt"}

def order_files_from_zones(zones: Dict[str, Set[str]]) -> List[Dict[str, str]]:
    ordered: List[Dict[str, str]] = []
    seen: Set[str] = set()

    def flush(group: Set[str]):
        for fp in sorted(group):
            if fp and fp not in seen:
                seen.add(fp)
                ordered.append({"file_path": fp})

    # Priority zones
    flush(zones.get("entrypoints", set()))
    flush(zones.get("defs", set()))
    flush({fp for fp in zones.get("upstream", set()) if not is_packaging(fp)})
    flush({fp for fp in zones.get("downstream", set()) if not is_packaging(fp)})

    # Packaging/closure last
    closure = set()
    for k in ("closure", "upstream", "downstream"):
        closure |= {fp for fp in zones.get(k, set()) if is_packaging(fp)}
    flush(closure)
    return ordered

# ====================
# Main entrypoint
# ====================

def retrieve_records_planner(question: str,
                             repo_name: str,
                             repo_overview: Dict[str, Any],
                             llm: GroqLLM) -> Dict[str, Any]:
    """
    Returns: {"files": [{"file_path": str}, ...]}
    """

    # Prepare class records
    providers = {
        "definition_location": lambda: records_provider_definition(repo_name),
        "entry_point_files":  lambda: records_provider_entrypoints(repo_name),
    }
    class_records = {k: providers[k]() for k in providers}

    # Router
    system_prompt = build_router_system(["definition_location", "entry_point_files"])
    router_prompt = build_router_prompt(repo_name, question, repo_overview)
    plan = safe_json(llm.generate(prompt=router_prompt, system_prompt=system_prompt, temperature=0.0))
    classes = [c for c in (plan.get("classes") or []) if c in ("definition_location", "entry_point_files")]
    hints = plan.get("hints", {}) if isinstance(plan.get("hints"), dict) else {}
    if not classes:
        # Safety duo when vague
        classes = ["entry_point_files", "definition_location"]

    # Gate by availability (drop classes with zero records)
    classes = [c for c in classes if class_records.get(c)]

    # Execute per class (LLM → ids)
    selected_ids: Dict[str, List[str]] = {}
    for cls in classes:
        recs = class_records.get(cls, [])
        out = exec_class_llm(question, cls, recs, llm, hints=hints.get(cls, []))
        selected_ids[cls] = [rid for rid in (out.get("records_used") or []) if isinstance(rid, str)]

    # Resolve to zones (Mongo-backed)
    zones = {"entrypoints": set(), "defs": set(), "upstream": set(), "downstream": set(), "closure": set()}
    definition_list = []
    if selected_ids.get("entry_point_files"):
        z = resolver_entrypoints(selected_ids["entry_point_files"], class_records.get("entry_point_files"))
        for k in zones: zones[k] |= z.get(k, set())
    if selected_ids.get("definition_location"):
        # z = resolver_definition(selected_ids["definition_location"])
        # for k in zones: zones[k] |= z.get(k, set())
        def_docs = fetch_defs_by_ids(selected_ids["definition_location"])
        for d in def_docs:
            loc = (d.get("location") or {})
            definition_list.append({
                "id": d.get("id"),
                "file_path": d.get("file_path"),
                "name": d.get("name"),
                "location": {
                    "start_line": loc.get("start_line"),
                    "end_line": loc.get("end_line"),
                    "start_column": loc.get("start_column"),
                    "end_column": loc.get("end_column"),
                },
                "dependencies": {
                    "upstream": d.get("dependencies", {}).get("upstream", []),
                    "downstream": d.get("dependencies", {}).get("downstream", []),
                }
            })

    # Order & return
    # files = order_files_from_zones(zones)
    # return {"files": files}

    # Prepare class-specific outputs (populate only routed classes; others empty)
    entrypoint_list = (
        [{"file_path": fp} for fp in sorted(zones.get("entrypoints", set()))]
        if "entry_point_files" in classes else []
    )

    return {
        "definition_records": definition_list,
        "entry_point_files": entrypoint_list,
    }


def _read_text(path: str) -> Optional[str]:
    try:
        from .tools.fetch_code_file import fetch_code_file
        return fetch_code_file(path)
    except Exception as exc:
        raise RuntimeError(f"Failed to read file {path}: {exc}")

def _slice_by_lines(src: str, start_line: int, end_line: int, pad: int = 3, hard_cap_lines: int = 160) -> Tuple[str, Tuple[int, int]]:
    """
    Extract lines [start_line, end_line] (1-based, inclusive), padded by `pad` lines on each side.
    Returns (snippet_text, (lo, hi)) with lo/hi being the actual 1-based line numbers in the slice.
    """
    lines = src.splitlines()
    n = len(lines)
    if start_line is None or end_line is None or start_line < 1 or end_line < start_line:
        # fall back: top of file
        lo, hi = 1, min(n, hard_cap_lines)
        return "\n".join(lines[lo-1:hi]), (lo, hi)

    lo = max(1, start_line - pad)
    hi = min(n, end_line + pad)
    # hard cap to avoid huge snippets
    if hi - lo + 1 > hard_cap_lines:
        # center around the definition span
        mid_lo = max(1, start_line - pad)
        mid_hi = min(n, start_line - pad + hard_cap_lines - 1)
        lo, hi = mid_lo, mid_hi
    return "\n".join(lines[lo-1:hi]), (lo, hi)

def answer_with_snippets(
    question: str,
    selection: Dict[str, Any],
    llm: GroqLLM,
    repo_root: Optional[str] = None,
    entrypoint_header_lines: int = 80,
) -> str:
    
    """
    Build an answer using targeted code snippets from the planner selection.

    Parameters
    ----------
    question : str
        The user's question.
    selection : dict
        Output from the planner, e.g. {
          "definition_records": [
            {"id": "...", "file_path": "...", "name": "...", "type": "...",
             "location": {"start_line": int, "end_line": int,
                          "start_column": int, "end_column": int}}
          ],
          "entry_point_files": [
            {"file_path": "..."},
            ...
          ]
        }
    llm : object
        Must implement .generate(prompt: str, system_prompt: Optional[str]=..., temperature: float=...)
    repo_root : Optional[str]
        If provided, file paths are resolved relative to this directory.
    entrypoint_header_lines : int
        Max number of header lines to include from each entry point file.

    Returns
    -------
    str
        The LLM's final answer text.
    """
    # Resolve path helper
    def _abs(p: str) -> str:
        if not repo_root:
            return p
        return os.path.join(repo_root, p) if not os.path.isabs(p) else p

    # Collect snippet blocks (as (label, snippet) tuples)
    blocks: List[Tuple[str, str]] = []

    # 1) Entry points: include a short header of the file
    for ep in selection.get("entry_point_files", []) or []:
        fp = ep.get("file_path")
        if not isinstance(fp, str):
            continue
        src = _read_text(fp)
        if not src:
            continue
        header = "\n".join(src.splitlines()[:max(1, entrypoint_header_lines)])
        label = f"{fp}  (entry_point header: first {min(entrypoint_header_lines, len(src.splitlines()))} lines)"
        blocks.append((label, header))

    # 2) Definitions: precise spans with a little padding
    for d in selection.get("definition_records", []) or []:
        fp = d.get("file_path")
        if not isinstance(fp, str):
            continue
        src = _read_text(fp)
        if not src:
            continue
        loc = d.get("location") or {}
        start_line = loc.get("start_line")
        end_line = loc.get("end_line")
        snippet, (lo, hi) = _slice_by_lines(src, start_line, end_line, pad=3, hard_cap_lines=16000)
        name = d.get("name") or ""
        dtype = d.get("type") or "definition"
        label = f"{fp}  ({dtype} {name})  [lines {lo}-{hi}]"

        blocks.append((label, snippet))

    if not blocks:
        yield "No code snippets could be retrieved to answer the question."
        return
    
    BUDGET = 40000
    sblocks: List[Tuple[str, str]] = []
    running = 0
    for label, snip in blocks:
        piece = f"# {label}\n```\n{snip}\n```\n"
        if running + len(piece) > BUDGET and sblocks:
            break
        sblocks.append((label, snip))
        running += len(piece)

    # Compose prompt
    system_prompt = (
        "You are a senior engineer. Use ONLY the provided code snippets and filenames to answer the question.\n"
        "- Be precise, cite filenames when relevant.\n"
        "- If a detail is not in the snippets, say what else would be needed.\n"
        "- Answer what is asked, nothing more nothing less"
    )
    parts = [f"QUESTION:\n{question}\n", "SNIPPETS:\n"]
    for label, snip in sblocks:
        parts.append(f"# {label}\n```\n{snip}\n```\n")
    prompt = "\n".join(parts)

    # Ask LLM
    qa_answer =llm.generate(prompt=prompt, system_prompt=system_prompt, temperature=0.0, stream=True)
    for chunk in qa_answer:
        content = chunk.choices[0].delta.content or ""
        if content:
            yield content