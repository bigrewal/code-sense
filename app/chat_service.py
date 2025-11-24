# chat_service.py

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import sys
from typing import Any, Dict, List, Optional

from .db import get_mongo_client
from .llm import GroqLLM
from .llm_grok import GrokLLM
from .tools import fetch_code_file
import tiktoken

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
        {"repo_id": repo_id, "document_type": "REPO_CONTEXT"},
        {"_id": 0, "context": 1},
    )
    repo_arch = (arch_doc or {}).get("context", "")

    system_seed = _make_system_seed(repo_arch, repo_id=repo_id)

    # def count_tokens(text: str, model: str = "gpt-4o") -> int:
    #     # Load the encoding for the given model
    #     encoding = tiktoken.encoding_for_model(model)
        
    #     # Encode the text into tokens
    #     tokens = encoding.encode(text)
        
    #     # Return number of tokens
    #     return len(tokens)
    
    # token_count = count_tokens(repo_arch)
    # print(f"Repository architecture token count: {token_count}")

    # yield f"**Repository Architecture Loaded.**\n\n"
    async for chunk in _stream_final_answer_grok(repo_id, user_message, system_seed):
        yield chunk

# ---------------------------
# Internal helpers
# ---------------------------

def _make_system_seed(file_summaries: str, repo_id: str) -> str:
    seed = (
        "Your task is to answer questions accurately about the given file summaries of the repo.\n"
        "Don't assume anything, Only answer from the information provided in the summaries. "
        " If a question warrants information that's not present say what file you need more information from.\n"
        "Use the repository architecture below to reason about *where* to look and how things fit together.\n"
        "When needed, you may inspect specific files (we will provide code). Be accurate, cite which files informed your answer.\n"
        f"REPOSITORY ID: {repo_id}\n\n"
        "FILE SUMMARIES:\n"
        "------------------------------------------------------------\n"
        f"{file_summaries.strip()}\n"
        "------------------------------------------------------------\n"
        "Answer clearly and precisely. If something is ambiguous, state assumptions or ask for a path to inspect."
    )
    return seed

async def _stream_final_answer_grok(
    repo_id: str,
    user_message: str,
    system_seed: str,
):
    llm = GrokLLM()

    user_prompt = (
        f"User Question:\n{user_message}\n\n"
        "Now produce the final answer."
        "Answer what is asked, nothing more nothing less."
    )

    captured: List[str] = []
    stream = llm.generate(
        prompt=user_prompt,
        system_prompt=system_seed,
        temperature=0.0,
        stream=True,
    )

    for response, chunk in stream:
        content = getattr(chunk, "content", None)
        if content:
            captured.append(content)
            print(content, end="", flush=True)  # or file=sys.stderr
            yield content

    yield "\n"

