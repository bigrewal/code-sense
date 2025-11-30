# chat_service.py

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import sys
from typing import Any, Dict, List, Optional

from bson import ObjectId

from .db import get_mongo_client
from .llm import GroqLLM
from .llm_grok import GrokLLM
from .tools import fetch_code_file
import tiktoken

MENTAL_MODEL_COL = "mental_model"
CONVERSATIONS_COL = "conversations"
MESSAGES_COL = "messages"


# ---------------------------
# Public API
# ---------------------------

async def stream_chat(conversation_id: str, user_message: str):
    """
    Stream a reply for a given conversation_id, ChatGPT-style.
    - Loads conversation → repo_id
    - Loads repo architecture (system seed)
    - Loads prior messages as history
    - Appends new user message
    - Streams assistant reply and saves both turns
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]
    conversations = mongo[CONVERSATIONS_COL]
    messages_col = mongo[MESSAGES_COL]

    # 1) Look up conversation to get repo_id
    conv = conversations.find_one({"_id": ObjectId(conversation_id)})
    if not conv:
        # You could also raise an HTTPException at the route level,
        # but for now we just stream an error message.
        yield "Conversation not found.\n"
        return

    repo_id = conv["repo_id"]

    # 2) Load repo architecture (seed for system prompt)
    arch_doc = mental.find_one(
        {"repo_id": repo_id, "document_type": "REPO_CONTEXT"},
        {"_id": 0, "context": 1},
    )
    repo_arch = (arch_doc or {}).get("context", "")

    system_seed = _make_system_seed(repo_arch, repo_id=repo_id)

    # 3) Load previous messages for this conversation
    history_cursor = messages_col.find(
        {"conversation_id": conversation_id}
    ).sort("created_at", 1)

    messages_for_llm: List[Dict[str, str]] = [
        {"role": "system", "content": system_seed}
    ]

    for m in history_cursor:
        messages_for_llm.append(
            {
                "role": m["role"],       # "user" or "assistant"
                "content": m["content"],
            }
        )

    # 4) Store this new user message in DB
    now = datetime.now(timezone.utc)
    messages_col.insert_one(
        {
            "conversation_id": conversation_id,
            "role": "user",
            "content": user_message,
            "created_at": now,
        }
    )

    # 5) Append new user message to LLM messages
    messages_for_llm.append(
        {
            "role": "user",
            "content": user_message,
        }
    )

    # 6) Stream assistant reply, capturing content so we can save it at the end
    captured: List[str] = []
    async for chunk in _stream_final_answer_grok(
        messages=messages_for_llm,
        captured=captured,
    ):
        yield chunk

    # 7) Save assistant message after streaming completes
    assistant_content = "".join(captured)
    messages_col.insert_one(
        {
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": assistant_content,
            "created_at": datetime.now(timezone.utc),
        }
    )


# ---------------------------
# Internal helpers
# ---------------------------

def _make_system_seed(file_summaries: str, repo_id: str) -> str:
    seed = (
        "Your task is to answer questions accurately about the given file summaries of the repo.\n"
        "Don't assume anything, Only answer from the information provided in the summaries. "
        " If a question warrants information that's not present say what file you need more information from.\n"
        "Use the file summaries below to reason about *where* to look and how things fit together.\n"
        "When needed, you may inspect specific files (we will provide code). Be accurate, cite which files informed your answer.\n"
        f"REPOSITORY ID: {repo_id}\n\n"
        "FILE SUMMARIES:\n"
        "------------------------------------------------------------\n"
        f"{file_summaries.strip()}\n"
        "------------------------------------------------------------\n"
        "Answer what is asked, nothing more nothing less. If something is ambiguous, state assumptions or ask for a path to inspect."
    )
    return seed

async def _stream_final_answer_grok(
    messages: List[Dict[str, str]],
    captured: List[str],
):
    """
    Stream the final answer using the full message history.
    `captured` is a mutable list passed in so the caller can
    persist the final assistant message after streaming.
    """
    llm = GrokLLM()

    stream = llm.generate(
        messages=messages,
        temperature=0.0,
        stream=True,
    )

    for response, chunk in stream:
        content = getattr(chunk, "content", None)
        if content:
            captured.append(content)
            # print(content, end="", flush=True, file=sys.stderr)
            yield content

    # Ensure a final newline is sent to the client
    yield "\n"


