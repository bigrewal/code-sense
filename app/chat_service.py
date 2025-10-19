# chat_service.py

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import get_mongo_client
from .llm import GroqLLM
from .tools import fetch_code_file

MENTAL_MODEL_COL = "mental_model"

# ~20k tokens ≈ 80k chars; keep headroom for prompts
MAX_CONTEXT_CHARS = 75000
# Hard caps for code ingestion
MAX_FILES_TO_READ = 6
MAX_CHARS_PER_FILE = 8000
MIN_CHARS_PER_FILE = 2000


# ---------------------------
# Public API
# ---------------------------

async def stream_chat(repo_id: str, user_message: str):
    """
    Agentic Q&A over a repository:
      1) Load repo architecture (system prompt seed).
      2) Ask LLM to PLAN which files to open (JSON).
      3) Fetch code for those files (bounded by budget).
      4) Ask LLM to ANSWER, streaming tokens.

    Yields markdown text chunks for streaming (async generator).
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]

    # 1) Load repo architecture (seed for system prompt)
    arch_doc = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_ARCHITECTURE_RETRIEVAL"},
        {"_id": 0, "architecture": 1, "entry_points": 1, "generated_at": 1},
    )
    repo_arch = (arch_doc or {}).get("architecture", "")
    entry_points = (arch_doc or {}).get("entry_points", [])
    arch_generated_at = (arch_doc or {}).get("generated_at", "")

    if not repo_arch:
        # Fallback to any repo summary if arch not present
        summary_doc = mental.find_one(
            {"repo_id": repo_id, "document_type": "REPO_SUMMARY"},
            {"_id": 0, "data": 1, "entry_points": 1},
        )
        repo_arch = (summary_doc or {}).get("data", "")
        entry_points = (summary_doc or {}).get("entry_points", entry_points)

    system_seed = _make_system_seed(repo_arch, entry_points, arch_generated_at)

    # 2) Planning pass — get candidate files to read
    plan = await _plan_files_to_read(repo_id, user_message, system_seed)
    files_to_read = _normalize_plan_files(plan, max_files=MAX_FILES_TO_READ)

    # 3) Fetch code under budget (sync fetch is fine; keep simple)
    code_contexts, truncated = _gather_code(repo_id, files_to_read)

    # 4) Stream the final answer
    async for chunk in _stream_final_answer(repo_id, user_message, system_seed, code_contexts, truncated):
        yield chunk


# ---------------------------
# Internal helpers
# ---------------------------

def _make_system_seed(repo_arch: str, entry_points: List[str], arch_generated_at: str) -> str:
    stamp = f"(architecture generated at {arch_generated_at}) " if arch_generated_at else ""
    ep_txt = ", ".join(entry_points) if entry_points else "N/A"
    seed = (
        "You are a senior software engineer answering questions about a specific code repository.\n"
        "Use the repository architecture below to reason about *where* to look and how things fit together.\n"
        "When needed, you may inspect specific files (we will provide code). Be accurate, cite which files informed your answer.\n"
        f"{stamp}ENTRY POINTS: {ep_txt}\n\n"
        "REPOSITORY ARCHITECTURE OVERVIEW:\n"
        "------------------------------------------------------------\n"
        f"{repo_arch.strip()}\n"
        "------------------------------------------------------------\n"
        "Answer clearly and precisely. If something is ambiguous, state assumptions or ask for a path to inspect."
    )
    return seed


async def _plan_files_to_read(repo_id: str, user_message: str, system_seed: str) -> Dict[str, Any]:
    """
    Ask the LLM to propose which files to open (by path) to answer the question.
    The LLM returns STRICT JSON:
      {
        "files": [{"path": "a/b.py", "why": "..."}, ...],
        "notes": ["...", "..."]
      }
    """
    llm = GroqLLM()
    system_prompt = (
        system_seed
        + "\n\nYou may propose a small set of files to inspect to answer the user's question.\n"
          "Return STRICT JSON only:\n"
          "{ \"files\": [{\"path\": <string>, \"why\": <string>} ...], \"notes\": [<string> ...] }\n"
          f"- Suggest up to {MAX_FILES_TO_READ} files initially (fewer is fine).\n"
          "- Paths must be exact and come from the repo.\n"
          "- If answering without code is enough, return an empty list for 'files'."
          "- Choose files that are strictly relevant to the question."
    )

    user_prompt = (
        f"User Question:\n{user_message}\n\n"
        "Plan which files (if any) should be inspected to answer the question thoroughly.\n"
        "Return JSON only; do not add extra text."
    )

    try:
        out = await llm.generate_async(
            prompt=user_prompt,
            system_prompt=system_prompt,
            reasoning_effort="medium",
            temperature=0.0,
        )
        text = (out or "").strip()
        obj = _safe_json(text)
        if not isinstance(obj, dict):
            return {"files": [], "notes": ["planner returned non-dict"]}
        files = obj.get("files") or []
        notes = obj.get("notes") or []
        # sanitize
        cleaned = []
        for it in files:
            path = (it.get("path") or "").strip()
            why = (it.get("why") or "").strip()
            if path:
                cleaned.append({"path": path, "why": why})
        return {"files": cleaned[:MAX_FILES_TO_READ], "notes": notes}
    except Exception as e:
        return {"files": [], "notes": [f"planner_error: {e}"]}


def _normalize_plan_files(plan: Dict[str, Any], max_files: int) -> List[str]:
    unique = []
    seen = set()
    for it in (plan.get("files") or []):
        p = it.get("path")
        if p and p not in seen:
            unique.append(p)
            seen.add(p)
        if len(unique) >= max_files:
            break
    return unique


def _gather_code(repo_id: str, files_to_read: List[str]) -> tuple[List[Dict[str, str]], bool]:
    """
    Fetch code for proposed files, keeping within a char budget.
    Returns (contexts, truncated_flag), where contexts = [{"path":..., "code":...}, ...]
    """
    contexts: List[Dict[str, str]] = []
    if not files_to_read:
        return contexts, False

    # distribute budget across files, with per-file caps
    n = len(files_to_read)
    per_file_budget = min(MAX_CHARS_PER_FILE, max(MIN_CHARS_PER_FILE, MAX_CONTEXT_CHARS // max(n, 1) - 2000))
    truncated_any = False

    for path in files_to_read:
        code = fetch_code_file(file_path=path) or ""
        # if len(code) > per_file_budget:
        #     code = code[:per_file_budget]
        #     truncated_any = True
        contexts.append({"path": path, "code": code})

    # If still above global budget, prune tail
    total = sum(len(c["code"]) + len(c["path"]) + 40 for c in contexts)
    if total > MAX_CONTEXT_CHARS:
        trimmed: List[Dict[str, str]] = []
        running = 0
        for c in contexts:
            need = len(c["code"]) + len(c["path"]) + 40
            if running + need > MAX_CONTEXT_CHARS:
                truncated_any = True
                break
            trimmed.append(c)
            running += need
        contexts = trimmed

    return contexts, truncated_any


async def _stream_final_answer(
    repo_id: str,
    user_message: str,
    system_seed: str,
    code_contexts: List[Dict[str, str]],
    truncated_any: bool,
):
    """
    Stream the final answer using the architecture seed + (optional) fetched code contexts.
    """
    llm = GroqLLM()

    # Build the composite prompt
    ctx_blocks = []
    for ctx in code_contexts:
        ctx_blocks.append(f"FILE: {ctx['path']}\nCODE:\n<<<\n{ctx['code']}\n>>>\n---")
    ctx_text = "\n".join(ctx_blocks)

    # Small header to always yield something early (keeps StreamingResponse happy)
    header = f"**Repo:** `{repo_id}`\n\n"
    if code_contexts:
        header += f"_Inspecting {len(code_contexts)} file(s){' (some truncated)' if truncated_any else ''}_\n\n"
    else:
        header += "_No files opened; answering from architecture overview._\n\n"
    yield header

    system_prompt = (
        system_seed
        + "\n\nWhen you answer:\n"
          "- Be precise and complete. If you made assumptions, state them.\n"
          "- Answer only based on the provided architecture and code snippets.\n"
          "- If code was provided, cite the specific file paths you used.\n"
          "- If the question asks for examples/fixes, include short, focused snippets."
          "- If you did not find relevant code, say so and answer from architecture only."
          "- If the question is not about the codebase, refuse to answer."
    )

    ctx_part = f"Relevant code contexts:\n{ctx_text}\n" if ctx_text else ""
    user_prompt = (
        f"User Question:\n{user_message}\n\n"
        f"{ctx_part}"
        "Now produce the final answer."
        "Answer what is asked, nothing more nothing less."
    )

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

    # (Optional) Persist transcript stub if you want basic logging
    try:
        mongo = get_mongo_client()
        logs = mongo.get_collection("chat_logs")
        logs.insert_one({
            "repo_id": repo_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "question": user_message,
            "files_opened": [c["path"] for c in code_contexts],
            "truncated_any": truncated_any,
            "answer_preview": "".join(captured)[:5000],
        })
    except Exception:
        pass


def _safe_json(text: str) -> Any:
    """
    Lenient JSON parser: attempt full parse, then fallback to first {...} block.
    """
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
        return {}
