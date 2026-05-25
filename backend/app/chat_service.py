# chat_service.py

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field

from .db import get_db_client
from .llm import LLMProvider, get_llm_provider
from .tools.fetch_code_file import fetch_code_file

MESSAGE_TYPE_CHAT = "chat_message"
MESSAGE_TYPE_PROGRESS = "progress_event"
logger = logging.getLogger(__name__)

_llm: LLMProvider | None = None


def _get_llm() -> LLMProvider:
    global _llm
    if _llm is None:
        _llm = get_llm_provider()
    return _llm

_repo_context_cache: dict[str, str] = {}
_repo_context_cache_lock = asyncio.Lock()


def invalidate_repo_context_cache(repo_name: str | None = None) -> None:
    """Clear cached repo context. Called after ingestion writes a new context."""
    if repo_name is None:
        _repo_context_cache.clear()
    else:
        _repo_context_cache.pop(repo_name, None)


async def _get_cached_repo_context(repo_name: str) -> str:
    cached = _repo_context_cache.get(repo_name)
    if cached is not None:
        return cached
    async with _repo_context_cache_lock:
        cached = _repo_context_cache.get(repo_name)
        if cached is not None:
            return cached
        db_client = get_db_client()
        context = await asyncio.to_thread(db_client.get_repo_context, repo_name)
        _repo_context_cache[repo_name] = context
        return context


class FileSelection(BaseModel):
    file_path: str
    info_needed: str


class FileSelectionResponse(BaseModel):
    files_to_fetch: list[FileSelection] = Field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChatEventRecorder:
    def __init__(self, conversation_id: str | None = None):
        self.conversation_id = conversation_id

    def _serialize(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, default=str) + "\n"

    def _event(self, event_type: str, **payload) -> str:
        return self._serialize({"type": event_type, **payload, "created_at": _utc_now().isoformat()})

    async def _persist_message(
        self,
        *,
        role: str,
        content: str,
        message_type: str,
        stage: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> None:
        if not self.conversation_id:
            return

        db_client = get_db_client()
        timestamp = created_at or _utc_now()
        await asyncio.to_thread(
            db_client.persist_message,
            conversation_id=self.conversation_id,
            role=role,
            content=content,
            message_type=message_type,
            stage=stage,
            status=status,
            metadata=metadata or {},
            created_at=timestamp,
        )

    async def persist_chat_message(self, *, role: str, content: str) -> None:
        await self._persist_message(
            role=role,
            content=content,
            message_type=MESSAGE_TYPE_CHAT,
        )

    async def emit_progress(
        self,
        *,
        stage: str,
        status: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        timestamp = _utc_now()
        metadata = metadata or {}
        await self._persist_message(
            role="assistant",
            content=message,
            message_type=MESSAGE_TYPE_PROGRESS,
            stage=stage,
            status=status,
            metadata=metadata,
            created_at=timestamp,
        )
        return self._serialize({
            "type": "progress",
            "stage": stage,
            "status": status,
            "message": message,
            "metadata": metadata,
            "created_at": timestamp.isoformat(),
        })

    def emit_content(self, delta: str) -> str:
        return self._event("content", delta=delta)

    def emit_error(self, message: str) -> str:
        return self._event("error", message=message)

    def emit_done(self) -> str:
        return self._event("done")

# ---------------------------
# Public API
# ---------------------------

async def stream_chat(conversation_id: str, user_message: str):
    db_client = get_db_client()
    recorder = ChatEventRecorder(conversation_id=conversation_id)
    try:
        conv = await asyncio.to_thread(db_client.get_conversation, conversation_id)
        if not conv:
            yield recorder.emit_error("Conversation not found.")
            return

        repo_name = conv["repo_name"]

        history_docs = await asyncio.to_thread(db_client.list_chat_history, conversation_id)
        messages_for_llm: list[dict[str, str]] = [
            {"role": m["role"], "content": m["content"]}
            for m in history_docs
            if m.get("role") in {"user", "assistant"}
        ]
        messages_for_llm.append({"role": "user", "content": user_message})

        await recorder.persist_chat_message(role="user", content=user_message)

        yield await recorder.emit_progress(
            stage="rephrasing_question",
            status="started",
            message="Rephrasing question",
        )
        rephrased_user_question = await get_rephrased_question(messages=messages_for_llm, repo_name=repo_name)
        logger.debug("Rephrased question for repo=%s", repo_name)
        yield await recorder.emit_progress(
            stage="rephrasing_question",
            status="completed",
            message="Question rephrased",
            metadata={"rephrased_question": rephrased_user_question},
        )

        captured: list[str] = []
        async for event in stream_answer(
            user_question=rephrased_user_question,
            repo_name=repo_name,
        ):
            if event["type"] == "content":
                delta = event["delta"]
                captured.append(delta)
                yield recorder.emit_content(delta)
                continue

            if event["type"] == "progress":
                yield await recorder.emit_progress(
                    stage=event["stage"],
                    status=event["status"],
                    message=event["message"],
                    metadata=event.get("metadata"),
                )
                continue

            yield recorder._serialize(event)

        assistant_content = "".join(captured)
        await recorder.persist_chat_message(role="assistant", content=assistant_content)
        yield recorder.emit_done()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Chat stream failed for conversation=%s", conversation_id)
        try:
            yield await recorder.emit_progress(
                stage="chat",
                status="failed",
                message="Chat failed",
                metadata={"error": str(exc)},
            )
        except Exception:
            logger.exception("Failed to persist chat failure for conversation=%s", conversation_id)
        yield recorder.emit_error("Chat failed. Please try again.")


async def stateless_stream_chat(repo_name: str, user_message: str):
    """
    Stream a reply for a given repo_name and user message, ChatGPT-style.
    """
    recorder = ChatEventRecorder()
    try:
        yield await recorder.emit_progress(
            stage="rephrasing_question",
            status="started",
            message="Rephrasing question",
        )
        rephrased_user_question = await get_rephrased_question(
            messages=[{"role": "user", "content": user_message}],
            repo_name=repo_name,
        )
        yield await recorder.emit_progress(
            stage="rephrasing_question",
            status="completed",
            message="Question rephrased",
            metadata={"rephrased_question": rephrased_user_question},
        )

        async for event in stream_answer(
            user_question=rephrased_user_question,
            repo_name=repo_name
        ):
            if event["type"] == "content":
                yield recorder.emit_content(event["delta"])
            else:
                yield recorder._serialize(event)

        yield recorder.emit_done()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Stateless chat stream failed for repo=%s", repo_name)
        yield recorder.emit_error("Chat failed. Please try again.")

# ---------------------------
# Internal helpers
# ---------------------------

async def get_rephrased_question(messages: list[dict[str, str]], repo_name: str):
    if len(messages) <= 2:
        return messages[-1]["content"]
    
    system_prompt = f"""You are a message rephraser. Your ONLY job is to rephrase the last user message into a standalone message.

        REPOSITORY NAME: {repo_name}

        CRITICAL RULES:
        - Output ONLY the rephrased message - nothing else
        - Do NOT answer the message
        - Do NOT provide information about the codebase
        - Do NOT say whether something exists or not
        - JUST rephrase the message to be self-contained

        Resolve pronouns and references using conversation context, but do not add any analysis or answers."""

    conversation_text = "\n".join(
        f"{msg['role'].upper()}: {msg['content']}" for msg in messages
    )
    
    user_prompt = f"""CONVERSATION:
    {conversation_text}

    Rephrase the LAST user message as a standalone question. Output ONLY the rephrased question:"""

    return await _get_llm().generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=0.0,
    )


async def stream_answer(user_question: str, repo_name: str) -> AsyncGenerator[dict[str, Any], None]:
    llm = _get_llm()

    async def _select_files_for_query(
        repo_context: str,
    ) -> tuple[list[FileSelection], str | None]:
        system_prompt = f"""
        You are a senior codebase analysis agent.

        Your task is to decide WHICH repository files (if any) must be examined in full
        in order to accurately answer a user's question about the codebase.

        REPOSITORY NAME: {repo_name}

        AVAILABLE CONTEXT
        ────────────────────────────────────────────
        FILE SUMMARIES (may be incomplete or lossy):
        {repo_context.strip()}
        ────────────────────────────────────────────

        Each file summary uses this exact structure:
        "`{{file_path}}` <what this file does and why it exists>. It does this by <how it works end-to-end, explicitly naming every major component/function/class defined in this file and each component's role>. It interacts upstream with <key files/modules that call or depend on this file> and downstream with <key files/modules/services this file calls or depends on>."

        The canonical file path for a summary is the exact backticked `file_path` at the start of that summary.

        TASK
        ────────────────────────────────────────────
        Given the user's question, determine:

        1. Whether the provided file summaries alone are sufficient to answer the question.
        2. If not, which specific files must be fetched and examined in full.
        3. For each selected file, specify precisely what information must be extracted.

        RULES
        ────────────────────────────────────────────
        - ONLY select files that are strictly necessary to answer the question.
        - Return ONLY valid JSON in this exact shape:
          {{"files_to_fetch":[{{"file_path":"...","info_needed":"..."}}]}}
        - If the summaries already provide enough information, return "files_to_fetch": [].
        - Do NOT guess or hallucinate code behavior.
        - Do NOT select files merely to “be safe” unless uncertainty would materially affect correctness.
        - Be explicit: name functions, classes, variables, or code paths when possible.
        - Prefer minimal file sets over broad coverage.
        - When selecting a file, copy the `file_path` exactly from the leading backticked path in the matching summary.
        - NEVER invent, rename, shorten, normalize, or infer a file path.
        - NEVER return a file path unless that exact path appears in the provided file summaries.

        GUIDANCE
        ────────────────────────────────────────────
        - High-level questions (architecture, responsibilities, design intent):
        → summaries are usually sufficient.
        - Questions about:
        - control flow
        - data transformations
        - side effects
        - integration points
        - correctness or bugs
        → usually require full file inspection.
        - If conflicting or ambiguous information exists in the summaries, select the authoritative source file.

        FAILURE MODES TO AVOID
        ────────────────────────────────────────────
        - Over-selecting files without a concrete reason
        - Vague information_needed entries (e.g., “check logic”)
        - Inferring implementation details not present in summaries
        - Mixing recommendation with speculation

        You are selecting files, not answering the user’s question.
        Accuracy and minimalism are more important than completeness.
        """

        
        response = ""
        try:
            response = await llm.generate(
                prompt=user_question,
                system_prompt=system_prompt,
                temperature=0.0,
                response_format=FileSelectionResponse,
            )
            logger.info("Raw file-selection LLM response: %r", response)

            return FileSelectionResponse.model_validate_json(response).files_to_fetch, None

        except Exception as exc:
            logger.error(
                "File selection failed for repo=%s; falling back to summary-only answer. raw_response=%r",
                repo_name,
                response,
                exc_info=True,
            )
            return [], f"{type(exc).__name__}: {exc}"
        
    async def _read_file_task(file_info: FileSelection) -> dict[str, Any]:
        try:
            code = fetch_code_file(repo_name=repo_name, file_path=file_info.file_path)

            user_prompt = f"""
                File path: {file_info.file_path}

                Code:
                {code}
                
                Information requested: {file_info.info_needed}

                """

            insight = await llm.generate(
                prompt=user_prompt,
                system_prompt="Your task is to only fetch the information requested from the provided code",
                temperature=0.0,
            )
            return {
                "file_path": file_info.file_path,
                "info_needed": file_info.info_needed,
                "result": {"file_path": file_info.file_path, "insight": insight},
                "error": None,
            }
        except Exception as exc:
            logger.exception("Error reading file %s", file_info.file_path)
            return {
                "file_path": file_info.file_path,
                "info_needed": file_info.info_needed,
                "result": None,
                "error": str(exc),
            }

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
            REPOSITORY NAME: {repo_name}

            FILE SUMMARIES:
            {repo_context}

            User question:
            {user_question}
            """
        
        return await llm.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.0,
        )

    async def _synth_final_answer(
        user_question: str,
        file_insights: list[Any],
        summary_insight: str,
    ):
        valid_file_insights = [
            insight
            for insight in file_insights
            if isinstance(insight, dict)
            and isinstance(insight.get("file_path"), str)
            and isinstance(insight.get("insight"), str)
        ]
        
        context_parts = []
        
        if valid_file_insights:
            context_parts.append("INSIGHTS FROM CODE FILES:")
            for insight in valid_file_insights:
                context_parts.append(f"[{insight['file_path']}]\n{insight['insight']}")
            context_parts.append("")
        
        if summary_insight:
            context_parts.append("INSIGHTS FROM REPOSITORY:")
            context_parts.append(summary_insight)
        
        gathered_context = "\n".join(context_parts)
        
        system_prompt = f"""
        You are a response synthesis agent for a codebase question-answering system.

        Your task is to produce the FINAL ANSWER to the user's question using ONLY the
        gathered context provided below.

        GATHERED CONTEXT (authoritative, do not reinterpret)
        ────────────────────────────────────────────
        {gathered_context}
        ────────────────────────────────────────────

        TASK
        ────────────────────────────────────────────
        Using the gathered context:

        - Present the information as a complete, self-contained answer to the user's question
        - Organize the material so it directly addresses the question being asked

        RULES (STRICT)
        ────────────────────────────────────────────
        - Answer the user's question coherently based on the gathered context.
        - Preserve exact wording for:
        - code snippets
        - function names
        - class names
        - variable names
        - file paths
        - Do NOT paraphrase or reinterpret technical statements.
        - If the context references specific files, explicitly cite them in the response.
        - If the context contains conflicting or divergent information, present ALL perspectives clearly.
        - Add ONLY minimal connective language needed for readability.
        - Do NOT introduce new information, explanations, opinions, or assumptions.
        - Do NOT rely on outside knowledge or inference.
        - Do NOT resolve conflicts unless the context itself does so.

        OUTPUT CONSTRAINTS
        ────────────────────────────────────────────
        - The answer must be fully grounded in the gathered context.
        - The response must neither exceed nor fall short of what the context supports.
        - Faithfulness to the context is more important than fluency or elegance.

        You are synthesizing, not analyzing or extending.
        """
        
        async for delta in llm.generate_stream(
            prompt=user_question,
            system_prompt=system_prompt,
            temperature=0.0,
        ):
            yield {"type": "content", "delta": delta}

        yield {"type": "content", "delta": "\n"}

    yield {
        "type": "progress",
        "stage": "selecting_files",
        "status": "started",
        "message": "Selecting relevant files",
        "metadata": {},
    }
    repo_context = await _get_cached_repo_context(repo_name)

    additional_info_required_task = asyncio.create_task(
        _select_files_for_query(repo_context=repo_context)
    )

    summary_task = asyncio.create_task(
        _answer_query_using_repo_context(
            repo_context=repo_context,
        )
    )

    additional_info_required, file_selection_error = await additional_info_required_task

    if file_selection_error:
        yield {
            "type": "progress",
            "stage": "selecting_files",
            "status": "failed",
            "message": "File selection failed; answering from repository summaries only.",
            "metadata": {"error": file_selection_error},
        }
    else:
        yield {
            "type": "progress",
            "stage": "selecting_files",
            "status": "completed",
            "message": "Completed file selection",
            "metadata": {
                "file_count": len(additional_info_required),
                "files": [item.file_path for item in additional_info_required],
            },
        }

    file_insights: list[Any] = []
    if additional_info_required:
        for file_info in additional_info_required:
            yield {
                "type": "progress",
                "stage": "reading_file",
                "status": "started",
                "message": f"Reading {file_info.file_path}",
                "metadata": {
                    "file_path": file_info.file_path,
                    "info_needed": file_info.info_needed,
                },
            }

        read_tasks = [
            asyncio.create_task(_read_file_task(file_info))
            for file_info in additional_info_required
        ]

        for task in asyncio.as_completed(read_tasks):
            outcome = await task
            if outcome["error"]:
                yield {
                    "type": "progress",
                    "stage": "reading_file",
                    "status": "failed",
                    "message": f"Failed reading {outcome['file_path']}",
                    "metadata": {
                        "file_path": outcome["file_path"],
                        "info_needed": outcome["info_needed"],
                        "error": outcome["error"],
                    },
                }
                continue

            file_insights.append(outcome["result"])
            yield {
                "type": "progress",
                "stage": "reading_file",
                "status": "completed",
                "message": f"Finished reading {outcome['file_path']}",
                "metadata": {
                    "file_path": outcome["file_path"],
                    "info_needed": outcome["info_needed"],
                },
            }

    yield {
        "type": "progress",
        "stage": "synthesizing_response",
        "status": "started",
        "message": "Synthesizing response",
        "metadata": {
            "file_count": len(file_insights),
        },
    }

    summary_insight = await summary_task

    async for chunk in _synth_final_answer(
        user_question=user_question,
        file_insights=file_insights,
        summary_insight=summary_insight
    ):
        yield chunk
    yield {
        "type": "progress",
        "stage": "synthesizing_response",
        "status": "completed",
        "message": "Response synthesis complete",
        "metadata": {
            "file_count": len(file_insights),
        },
    }
