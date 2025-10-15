# reverse_eng_service.py
#
# Purpose: Reverse-engineer a repository into an implementation-ready document.
# Simplified execution model:
#   - Traverse the whole repo (downstream from all entry points; no depth cap).
#   - For EVERY file (with a BRIEF_FILE_OVERVIEW), do ONE LLM CALL PER FILE.
#   - Run up to 4 file calls concurrently (no token packing needed).
#   - Merge per-file specs, then do ONE final LLM call to write the doc.
#   - Persist JSON spec + Markdown doc + coverage.

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Tuple

from .db import get_mongo_client, get_neo4j_client, get_entry_point_files
from .llm import GroqLLM
from .tools import fetch_code_file

MENTAL_MODEL_COL = "mental_model"
CONCURRENCY = 4  # run 4 files at a time


# ---------------------------
# Public API
# ---------------------------

async def build_reverse_spec(repo_id: str) -> Dict[str, Any]:
    """
    Reverse-engineer the repo to an implementation-ready spec and document.

    Steps:
      1) Load entry points and all files that have BRIEF_FILE_OVERVIEW.
      2) Full downstream traversal from all EPs (no depth cap).
      3) Ensure coverage by including any brief-file not in traversal (stable union).
      4) PER-FILE reverse-engineering (one LLM call per file), with concurrency=4.
      5) Merge to repo-wide JSON spec; compute flows; validate coverage.
      6) Final LLM pass to create a coherent Markdown reverse-engineering document.
      7) Persist to Mongo under document_type="REVERSE_SPEC".
    """
    mongo = get_mongo_client()
    neo = get_neo4j_client()
    mental = mongo[MENTAL_MODEL_COL]

    # 1) Entry points + all BRIEF_FILE_OVERVIEW docs
    entry_points = get_entry_point_files(mongo, repo_id) or []
    brief_map = _load_all_briefs(mental, repo_id)  # {file_path: summary}
    brief_files_all: Set[str] = set(brief_map.keys())
    if not brief_files_all:
        raise ValueError(f"No BRIEF_FILE_OVERVIEW documents found for repo {repo_id}")

    # 2) Full traversal from EPs (no depth cap)
    traversed = _full_bfs(repo_id, entry_points, neo)

    # 3) Ensure coverage: add any brief files not reached by traversal
    files_ordered = _stable_union_order(traversed, brief_files_all)

    # 4) PER-FILE reverse-engineering (one LLM call per file), concurrency=4
    per_file_specs = await _extract_all_file_specs(repo_id, files_ordered, brief_map, neo, concurrency=CONCURRENCY)

    # 5) Merge + coverage validation
    merged_spec, extracted_paths = _merge_file_specs(per_file_specs, entry_points)
    missing_files = sorted(list(brief_files_all - extracted_paths))

    # 6) Final doc
    reverse_doc_md = await _reduce_reverse_doc(repo_id, entry_points, merged_spec, missing_files)

    # 7) Persist
    doc = {
        "repo_id": repo_id,
        "document_type": "REVERSE_SPEC",
        "entry_points": entry_points,
        "reverse_spec_json": merged_spec,
        "reverse_doc_md": reverse_doc_md,
        "coverage": {
            "brief_files_total": len(brief_files_all),
            "extracted_files_total": len(extracted_paths),
            "missing_files": missing_files,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "reverse_eng_v3_per_file_async",
        "concurrency": CONCURRENCY,
    }
    mental.update_one(
        {"repo_id": repo_id, "document_type": "REVERSE_SPEC"},
        {"$set": doc},
        upsert=True,
    )
    return doc


# ---------------------------
# Data loading & traversal
# ---------------------------

def _load_all_briefs(mental, repo_id: str) -> Dict[str, str]:
    cur = mental.find(
        {"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW"},
        {"_id": 0, "file_path": 1, "data": 1},
    )
    return {d["file_path"]: (d.get("data") or "") for d in cur}

def _full_bfs(repo_id: str, entry_points: List[str], neo) -> List[str]:
    """
    Full downstream BFS from all entry points (no depth cap).
    Returns deterministic order (queue order + alpha sort of children).
    """
    visited: Set[str] = set()
    order: List[str] = []

    from collections import deque
    q = deque()
    for ep in entry_points:
        q.append(ep)

    while q:
        fp = q.popleft()
        if fp in visited:
            continue
        visited.add(fp)
        order.append(fp)

        try:
            _up, downstream = neo.file_dependencies(repo_id=repo_id, file_path=fp)
        except Exception:
            downstream = []
        for child in sorted(set(downstream)):
            if child not in visited:
                q.append(child)

    return order

def _stable_union_order(primary: List[str], all_files: Set[str]) -> List[str]:
    """
    Keep 'primary' order first, then append any remaining files (sorted) to ensure full coverage.
    """
    seen = set(primary)
    rest = sorted([f for f in all_files if f not in seen])
    return primary + rest


# ---------------------------
# Per-file extraction (async)
# ---------------------------

async def _extract_all_file_specs(
    repo_id: str,
    files_ordered: List[str],
    brief_map: Dict[str, str],
    neo,
    concurrency: int = 4,
) -> List[Dict[str, Any]]:
    """
    Extract per-file specs using ONE LLM CALL PER FILE, in parallel (bounded by `concurrency`).
    """
    sem = asyncio.Semaphore(concurrency)

    async def run_one(fp: str) -> Dict[str, Any]:
        async with sem:
            return await _extract_file_spec(repo_id, fp, brief_map.get(fp, ""), neo)

    tasks = [asyncio.create_task(run_one(fp)) for fp in files_ordered]
    results: List[Dict[str, Any]] = []
    for t in asyncio.as_completed(tasks):
        results.append(await t)
    return results


async def _extract_file_spec(repo_id: str, file_path: str, brief: str, neo) -> Dict[str, Any]:
    """
    ONE FILE → ONE CALL.
    Returns a STRICT-JSON spec describing the file's behavior.
    Shape:
      {
        "file_path": str,
        "inputs": [{name,type,source,desc}],
        "outputs": [{name,type,sink,desc}],
        "side_effects": [str],
        "algorithm": {"name": str, "pseudocode": str},
        "data_models": [{"name": str, "shape": str, "fields":[{name,type,desc}]}],
        "preconditions": [str],
        "postconditions": [str],
        "error_handling": [str],
        "dependencies_used": [str],
        "confidence": "low|medium|high",
        "notes": [str, ...]
      }
    """
    try:
        upstream, downstream = neo.file_dependencies(repo_id=repo_id, file_path=file_path)
    except Exception:
        upstream, downstream = [], []

    code = fetch_code_file(file_path=file_path) or ""

    llm = GroqLLM()
    system_prompt = (
        "You are reverse-engineering a single source file to capture its exact behavior (not language trivia).\n"
        "Extract a precise, implementation-ready specification with:\n"
        "- Inputs (names/types/sources), Outputs (names/types/sinks)\n"
        "- Side effects, preconditions, postconditions, error handling\n"
        "- Key algorithm (name + pseudocode sufficient to re-implement)\n"
        "- Data models (shape/fields) and dependencies used\n"
        "Use ONLY the provided brief summary, dependency lists, and code.\n"
        "Return STRICT JSON with the keys described."
    )
    user_prompt = (
        f"REPO_ID: {repo_id}\n"
        f"FILE_PATH: {file_path}\n\n"
        f"BRIEF_FILE_OVERVIEW:\n{(brief or '').strip()}\n\n"
        f"UPSTREAM_DEPENDENCIES: {upstream}\n"
        f"DOWNSTREAM_DEPENDENCIES: {downstream}\n\n"
        "CODE:\n<<<\n"
        f"{code}\n"
        ">>>\n\n"
        "Return STRICT JSON only."
    )

    out = await llm.generate_async(
        prompt=user_prompt,
        system_prompt=system_prompt,
        reasoning_effort="medium",
        temperature=0.0,
    )
    text = (out or "").strip()

    spec = _safe_json_default(text, {})
    # Normalize minimal shape and attach path if missing
    if not isinstance(spec, dict):
        spec = {}
    spec.setdefault("file_path", file_path)
    spec.setdefault("inputs", [])
    spec.setdefault("outputs", [])
    spec.setdefault("side_effects", [])
    spec.setdefault("algorithm", {"name": "", "pseudocode": ""})
    spec.setdefault("data_models", [])
    spec.setdefault("preconditions", [])
    spec.setdefault("postconditions", [])
    spec.setdefault("error_handling", [])
    spec.setdefault("dependencies_used", downstream or [])
    spec.setdefault("confidence", "medium")
    spec.setdefault("notes", [])
    return spec


# ---------------------------
# Merge & flows
# ---------------------------

def _merge_file_specs(per_file_specs: List[Dict[str, Any]], entry_points: List[str]) -> Tuple[Dict[str, Any], Set[str]]:
    """
    Merge all file specs into a single repo spec and compute naive flows from EPs.
    Returns (merged_spec, extracted_file_paths).
    """
    files_by_path: Dict[str, Dict[str, Any]] = {}
    data_models_index: Dict[str, Dict[str, Any]] = {}
    algorithms: List[Dict[str, Any]] = []
    notes: List[str] = []
    extracted_paths: Set[str] = set()

    def score(c: str) -> int:
        return {"low": 0, "medium": 1, "high": 2}.get((c or "").lower(), 1)

    for f in per_file_specs:
        if not isinstance(f, dict):
            continue
        fp = f.get("file_path")
        if not fp:
            continue
        extracted_paths.add(fp)

        prev = files_by_path.get(fp)
        if not prev or score(f.get("confidence")) >= score(prev.get("confidence")):
            files_by_path[fp] = f

        alg = f.get("algorithm") or {}
        if alg.get("name") or alg.get("pseudocode"):
            algorithms.append({"file_path": fp, "name": alg.get("name", ""), "pseudocode": alg.get("pseudocode", "")})

        for dm in f.get("data_models") or []:
            name = (dm.get("name") or "").strip()
            if not name:
                continue
            if name not in data_models_index:
                data_models_index[name] = dm
            else:
                existing = data_models_index[name]
                seen = {(fld.get("name"), fld.get("type")) for fld in existing.get("fields", [])}
                for fld in dm.get("fields", []):
                    key = (fld.get("name"), fld.get("type"))
                    if key not in seen:
                        existing.setdefault("fields", []).append(fld)

        for n in f.get("notes") or []:
            if isinstance(n, str) and n.strip():
                notes.append(n.strip())

    # Build naive flows from entry points following dependencies_used
    flows = []
    for ep in entry_points:
        steps = []
        seen: Set[str] = set()
        frontier = [ep]
        guard = 0
        while frontier and guard < 10000:
            nxt: List[str] = []
            for fp in frontier:
                if fp in seen:
                    continue
                seen.add(fp)
                spec = files_by_path.get(fp)
                if spec:
                    steps.append({
                        "file_path": fp,
                        "inputs": spec.get("inputs") or [],
                        "outputs": spec.get("outputs") or [],
                        "side_effects": spec.get("side_effects") or [],
                    })
                    for d in (spec.get("dependencies_used") or []):
                        if isinstance(d, str) and d not in seen:
                            nxt.append(d)
            frontier = nxt
            guard += 1
        flows.append({"entry_point": ep, "steps": steps})

    merged = {
        "schema_version": 1,
        "entry_points": entry_points,
        "files": list(files_by_path.values()),
        "data_models": list(data_models_index.values()),
        "algorithms": algorithms,
        "flows": flows,
        "notes": notes,
    }
    return merged, extracted_paths


# ---------------------------
# Reduce: final document
# ---------------------------

async def _reduce_reverse_doc(
    repo_id: str,
    entry_points: List[str],
    spec: Dict[str, Any],
    missing_files: List[str],
) -> str:
    llm = GroqLLM()
    # Clip massive JSON if necessary (not usually needed since per-file calls were used)
    spec_json = json.dumps(spec)[:200_000]  # generous cap; adjust if your LLM needs smaller

    coverage_note = ""
    if missing_files:
        coverage_note = (
            "\n\n> NOTE: The following files did not produce extracted specs and may need manual inspection:\n"
            + "\n".join(f"- {fp}" for fp in missing_files[:300])
        )

    system_prompt = (
        "You are producing a reverse-engineering document that allows a competent engineer to re-implement the repository.\n"
        "Focus on BEHAVIOR and TRANSFORMATIONS, not language trivia.\n"
        "Include:\n"
        "1) Overview & entry points (how execution starts)\n"
        "2) Inputs & outputs (types/sources/sinks), pre/postconditions, error behavior\n"
        "3) Core data models (shapes & fields)\n"
        "4) Key algorithms with clear pseudocode sufficient to implement\n"
        "5) End-to-end flows (per entry point) mapping input → transformations → output\n"
        "6) External side effects (files, network, DB, env)\n"
        "Be specific, concise, and non-redundant. Assume the reader wants to re-create the system from scratch."
    )
    user_prompt = (
        f"REPO_ID: {repo_id}\n"
        f"ENTRY_POINTS: {entry_points}\n\n"
        f"MERGED_SPEC_JSON:\n{spec_json}\n"
        f"{coverage_note}\n\n"
        "Write the final Markdown reverse-engineering document now."
    )

    out = await llm.generate_async(
        prompt=user_prompt,
        system_prompt=system_prompt,
        reasoning_effort="medium",
        temperature=0.0,
    )
    return (out or "").strip()


# ---------------------------
# Utils
# ---------------------------

def _safe_json_default(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return default
        return default
