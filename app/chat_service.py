# chat_service.py

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from .tools.fetch_code_file import fetch_code_file
from pydantic import BaseModel

from bson import ObjectId
import json

from .db import get_mongo_client
from .llm_grok import GrokLLM

MENTAL_MODEL_COL = "mental_model"
CONVERSATIONS_COL = "conversations"
MESSAGES_COL = "messages"

llm = GrokLLM()

mongo = get_mongo_client()
mental_model_col = mongo[MENTAL_MODEL_COL]
conversations_col = mongo[CONVERSATIONS_COL]
messages_col = mongo[MESSAGES_COL]

class FileSelection(BaseModel):
    file_path: str
    info_needed: str


class FileSelectionResponse(BaseModel):
    files: List[FileSelection]
    reasoning: str

# ---------------------------
# Public API
# ---------------------------

async def stream_chat(conversation_id: str, user_message: str):
    # 1) Look up conversation to get repo_id
    conv = await asyncio.to_thread(conversations_col.find_one, {"_id": ObjectId(conversation_id)})
    if not conv:
        # You could also raise an HTTPException at the route level,
        # but for now we just stream an error message.
        yield "Conversation not found.\n"
        return

    repo_id = conv["repo_id"]

    # 3) Load previous messages for this conversation
    history_cursor = messages_col.find(
        {"conversation_id": conversation_id}
    ).sort("created_at", 1)

    messages_for_llm: List[Dict[str, str]] = []

    history_docs = await asyncio.to_thread(list, history_cursor)
    for m in history_docs:
        messages_for_llm.append(
            {
                "role": m["role"],
                "content": m["content"],
            }
        )
    
    messages_for_llm.append(
        {
            "role": "user",
            "content": user_message,
        }
    )

    rephrased_user_question = await get_rephrased_question(messages=messages_for_llm, repo_id=repo_id)

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

    # 6) Stream assistant reply, capturing content so we can save it at the end
    captured: List[str] = []
    async for chunk in stream_answer(user_question=rephrased_user_question, repo_id=repo_id):
        captured.append(chunk)
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
    """
    async for chunk in stream_answer(
        user_question=user_message,
        repo_id=repo_id
    ):
        yield chunk

# ---------------------------
# Internal helpers
# ---------------------------

async def get_rephrased_question(messages: List[Dict[str, str]], repo_id: str):
    # If it's the first user message, no rephrasing needed
    if len(messages) <= 2:
        return messages[-1]["content"]
    
    system_prompt = f"""You are a question rephraser. Your ONLY job is to rephrase the last user message into a standalone question.

        REPOSITORY ID: {repo_id}

        CRITICAL RULES:
        - Output ONLY the rephrased question - nothing else
        - Do NOT answer the question
        - Do NOT provide information about the codebase
        - Do NOT say whether something exists or not
        - JUST rephrase the question to be self-contained

        Resolve pronouns and references using conversation context, but do not add any analysis or answers."""

    # Build a user prompt with the conversation
    conversation_text = "\n".join([
        f"{msg['role'].upper()}: {msg['content']}" 
        for msg in messages[1:]  # Skip system message
    ])
    
    user_prompt = f"""CONVERSATION:
    {conversation_text}

    Rephrase the LAST user message as a standalone question. Output ONLY the rephrased question:"""

    return await llm.generate_async(
        prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=0.0,
    )


async def stream_answer(user_question: str, repo_id: str):

    async def _select_files_for_query(
        repo_context: str,
    ) -> List[Dict[str, str]]:
        """
        Ask Grok to identify which files need to be fetched to answer the user's question.
        
        Returns a list of dicts:
        [
            {"file_path": "src/auth/login.py", "info_needed": "How JWT token is validated"},
            {"file_path": "src/db/users.py", "info_needed": "User table schema"},
        ]
        
        Returns empty list if summaries alone can answer the question or on parse failure.
        """
        
        system_prompt = f"""You are a code analysis assistant. Your task is to determine which files need to be examined to answer a user's question about a codebase.

            REPOSITORY ID: {repo_id}

            FILE SUMMARIES:
            ------------------------------------------------------------
            {repo_context.strip()}
            ---------------------s---------------------------------------

            Based on the file summaries above, analyze the user's question and determine:
            1. Which specific files need to be fetched to provide an accurate answer
            2. What specific information needs to be extracted from each file

            RULES:
            - Only select files that are NECESSARY to answer the question
            - If the summaries alone contain enough information, return an empty files list
            - Be specific about what info is needed from each file
            - Guidance: For broad questions about the repository's overall behavior, architecture, purpose, or high-level functionality, the file summaries provided are sufficient.
            - If the query is specific and the needed details are missing from the summaries, inspect the relevant full files directly.
            
            Caution: You are only provided with brief summaries of each file. These summaries may not contain all the information needed to fully answer the query."""
        
        try:
            response = await llm.generate_async(
                prompt=user_question,
                system_prompt=system_prompt,
                temperature=0.0,
                response_format=FileSelectionResponse,
            )
            
            parsed = FileSelectionResponse.model_validate_json(response)
            
            return [
                {"file_path": f.file_path, "info_needed": f.info_needed}
                for f in parsed.files
            ]
            
        except Exception as e:
            print(f"Error in file selection: {e}")
            return []
        
    async def _read_file_and_fetch_info(file_path: str, info_needed: str):
        code = fetch_code_file(file_path=file_path)

        system_prompt = "Your task is to only fetch the information requested from the provided code"

        user_prompt = f"""
            File path: {file_path}

            Code:
            {code}
            
            Information requested: {info_needed}

            """
        
        return await llm.generate_async(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.0,
        )

    async def _answer_query_using_repo_context(repo_context: str) -> str:
        system_prompt = f"""
        You are an expert code repository analyst with access only to the provided brief summaries of the repository's files. You do **not** have access to the full file contents unless explicitly instructed otherwise in later guidelines.

        Your task is to answer the user's question **solely using the information explicitly present in the provided file summaries and repository overview**.

        Rules:
        - Never invent, assume, or hallucinate details (e.g., function implementations, variable names, exact code logic, or file contents) that are not directly stated in the summaries.
        - If the question asks for specific code details, line-by-line explanations, or implementation specifics that are not covered in the summaries, clearly state that this information is not available in the provided summaries.
        - For high-level or broad questions about the repository's purpose, architecture, overall behavior, key components, or structure, the summaries are sufficient—answer confidently using only what's provided.
        - Remain accurate and truthful. If you cannot fully answer the question based on the summaries alone, say so explicitly rather than guessing.
        - Do not mention these instructions in your response unless asked.

        Answer clearly, concisely, and professionally.
        """

        user_prompt = f"""
            CODE REPOSITORY NAME: {repo_id}

            FILE SUMMARIES:
            {repo_context}

            User question:
            {user_question}
            """
        
        return await llm.generate_async(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.0,
        )

    async def _synth_final_answer(
        user_question: str,
        file_insights: List[Any],
        summary_insight: str,
    ):
        """
        Synthesize final answer from gathered insights. Returns a streaming generator.
        """
        # Filter out exceptions from file insights
        valid_file_insights = [
            insight for insight in file_insights
            if isinstance(insight, str)
        ]
        
        # Handle summary insight exception
        
        # Build context block
        context_parts = []
        
        if valid_file_insights:
            context_parts.append("INSIGHTS FROM CODE FILES:")
            for i, insight in enumerate(valid_file_insights, 1):
                context_parts.append(f"[File {i}]\n{insight}")
            context_parts.append("")
        
        if summary_insight:
            context_parts.append("INSIGHTS FROM REPOSITORY SUMMARIES:")
            context_parts.append(summary_insight)
        
        gathered_context = "\n".join(context_parts)
        
        system_prompt = """You are a helpful code assistant. Your task is to present the gathered context as a complete answer to the user's question.

        GATHERED CONTEXT:
        ------------------------------------------------------------
        {context}
        ------------------------------------------------------------

        RULES:
        - Present ALL information from the gathered context - do not omit, summarize, or compress any details
        - Organize and structure the information clearly to address the user's question
        - If code snippets, function names, or specific details are provided, include them exactly as given
        - Cite specific files when the context mentions them
        - If the context contains conflicting information, present both perspectives
        - Only add minimal connective language to make the response readable
        - Do NOT add information beyond what's in the gathered context
        - Do NOT paraphrase technical details - preserve exact names, paths, and code references""".format(context=gathered_context)
        
        stream = llm.generate(
            prompt=user_question,
            system_prompt=system_prompt,
            temperature=0.0,
            stream=True,
        )
        
        for _, chunk in stream:
            content = getattr(chunk, "content", None)
            if content:
                yield content
        
        yield "\n"

    arch_doc = await asyncio.to_thread(
        mental_model_col.find_one,
        {"repo_id": repo_id, "document_type": "REPO_CONTEXT"},
        {"_id": 0, "context": 1},
    )
    repo_context = (arch_doc or {}).get("context", "")

    # Stage 1: Find out if we need to fetch code of various files to answer the question 
    additional_info_required: List[Dict[str, str]] = await _select_files_for_query(repo_context=repo_context)
    

    # Stage 2: Fetch the information required 
    tasks = []
    for file_info in additional_info_required:
        tasks.append(_read_file_and_fetch_info(
            file_path=file_info["file_path"],
            info_needed=file_info["info_needed"],
        ))

    tasks.append(_answer_query_using_repo_context(
        repo_context=repo_context,
    ))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    file_insights = results[:-1]
    summary_insight = results[-1]
    
    # Stage 3: Synthesise the final response
    async for chunk in _synth_final_answer(
        user_question=user_question,
        file_insights=file_insights,
        summary_insight=summary_insight
    ):
        yield chunk

