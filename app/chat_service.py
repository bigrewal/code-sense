# chat_service.py

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from bson import ObjectId

from .db import get_mongo_client
from .llm_grok import GrokLLM

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
    conv = await asyncio.to_thread(conversations.find_one, {"_id": ObjectId(conversation_id)})
    if not conv:
        # You could also raise an HTTPException at the route level,
        # but for now we just stream an error message.
        yield "Conversation not found.\n"
        return

    repo_id = conv["repo_id"]

    # 2) Load repo architecture (seed for system prompt)
    arch_doc = await asyncio.to_thread(
        mental.find_one,
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

    history_docs = await asyncio.to_thread(list, history_cursor)
    for m in history_docs:
        messages_for_llm.append(
            {
                "role": m["role"],       # "user" or "assistant"
                "content": m["content"],
            }
        )

    # 4) Store this new user message in DB
    now = datetime.now(timezone.utc)
    await asyncio.to_thread(
        messages_col.insert_one,
        {
            "conversation_id": conversation_id,
            "role": "user",
            "content": user_message,
            "created_at": now,
        },
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
    await asyncio.to_thread(
        messages_col.insert_one,
        {
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": assistant_content,
            "created_at": datetime.now(timezone.utc),
        },
    )


async def stateless_stream_chat(repo_id: str, user_message: str):
    """
    Stream a reply for a given repo_id and user message, ChatGPT-style.
    This is a stateless chat (no conversation history).
    - Loads repo architecture (system seed)
    - Streams assistant reply
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]

    # 1) Load repo architecture (seed for system prompt)
    arch_doc = await asyncio.to_thread(
        mental.find_one,
        {"repo_id": repo_id, "document_type": "REPO_CONTEXT"},
        {"_id": 0, "context": 1},
    )
    repo_arch = (arch_doc or {}).get("context", "")

    system_seed = _make_system_seed(repo_arch, repo_id=repo_id)

    # 2) Build messages for LLM
    messages_for_llm: List[Dict[str, str]] = [
        {"role": "system", "content": system_seed},
        {
            "role": "user",
            "content": user_message,
        }
    ]

    # 3) Stream assistant reply, capturing content so we can save it at the end
    captured: List[str] = []
    async for chunk in _stream_final_answer_grok(
        messages=messages_for_llm,
        captured=captured,
    ):
        yield chunk

# ---------------------------
# Internal helpers
# ---------------------------

def _make_system_seed(file_summaries: str, repo_id: str) -> str:
    seed = (
        f"REPOSITORY ID: {repo_id}\n\n"
        "FILE SUMMARIES:\n"
        "------------------------------------------------------------\n"
        f"{file_summaries.strip()}\n"
        "------------------------------------------------------------\n\n"
        "Your job is to provide complete and accurate answers to user questions using these file summaries.\n"
        "If the answer is not contained in these summaries then say what more info you need and from where. "
        "Be accurate, cite which files informed your answer.\n"
        "Answer what is asked, nothing more nothing less. If something is ambiguous, state assumptions or ask for a path to inspect."
        "Use your internal reasoning and knowledge + provided summaries to answer the question.\n"
        "In the reponse don't mention that you got the info from file summaries, just provide the answer.\n"
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
