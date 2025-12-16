# llm_grok.py

import logging
import time
import asyncio
import traceback
from typing import Optional, Dict, List, Any

import grpc
from xai_sdk import Client, AsyncClient
from xai_sdk.chat import system as system_message
from xai_sdk.chat import user as user_message
from xai_sdk.chat import assistant as assistant_message

from .config import Config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("grpc").setLevel(logging.WARNING)


def _is_rate_limit_error(exc: Exception) -> bool:
    """
    Best-effort detection of xAI rate-limit style errors.

    xai-sdk uses gRPC; rate limits typically surface as RESOURCE_EXHAUSTED.
    We also fall back to string matching for 429 / 'rate limit'.
    """
    # gRPC sync error
    if isinstance(exc, grpc.RpcError):
        code = exc.code()
        if code in (grpc.StatusCode.RESOURCE_EXHAUSTED,):
            return True

    # gRPC async error (AioRpcError)
    aio = getattr(grpc, "aio", None)
    if aio is not None and isinstance(exc, aio.AioRpcError):
        code = exc.code()
        if code in (grpc.StatusCode.RESOURCE_EXHAUSTED,):
            return True

    # Fallback string checks
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "resource_exhausted" in msg:
        return True

    return False


def _normalise_content(content: Any) -> str:
    """
    Convert OpenAI-style content (which may be a string or a list of segments)
    into a plain string. This keeps things simple and text-only.
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                # e.g. {"type": "text", "text": "..."}
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                parts.append(str(part))
        return "".join(parts)

    return str(content)


def _convert_messages(messages: List[Any]) -> List[Any]:
    """
    Convert OpenAI-style message dicts into xai_sdk.chat message objects.

    Supported roles:
      - system -> system_message(...)
      - user -> user_message(...)
      - assistant -> assistant_message(...)

    If a message is not a dict (i.e. already a xai_sdk.chat message object),
    it is passed through unchanged.
    """
    converted: List[Any] = []

    for msg in messages:
        # Already an xai_sdk.chat message object?
        if not isinstance(msg, dict):
            converted.append(msg)
            continue

        role = msg.get("role")
        content = _normalise_content(msg.get("content", ""))

        if role == "system":
            converted.append(system_message(content))
        elif role == "user":
            converted.append(user_message(content))
        elif role == "assistant":
            converted.append(assistant_message(content))
        else:
            raise ValueError(f"Unsupported message role for xai-sdk: {role!r}")

    return converted


class GrokLLM:
    """
    xAI Grok-backed LLM wrapper with the same public interface as GroqLLM.

    - Uses xai_sdk.Client / AsyncClient.
    - Stateless from the caller's POV (each generate() call builds a fresh chat).
    """

    def __init__(self):
        # Let xai-sdk read XAI_API_KEY from env by default.
        client_kwargs: Dict[str, Any] = {}

        # If you define Config.XAI_API_KEY, we’ll use it; otherwise env will be used.
        if hasattr(Config, "XAI_API_KEY"):
            client_kwargs["api_key"] = Config.XAI_API_KEY

        # You can optionally add channel_options / timeout here if desired.
        self.client = Client(**client_kwargs)
        self.async_client = AsyncClient(**client_kwargs)

    def _build_chat(
        self,
        model: str,
        prompt: str,
        system_prompt: str,
        messages: Optional[List[Any]],
        temperature: Optional[float],
        max_tokens: Optional[int],
        response_format: Optional[Dict[str, Any]],
        reasoning_effort: Optional[str],
        tools: Optional[list],
        tool_choice: Optional[str],
    ):
        """
        Internal helper to create a chat object and append initial messages.
        """
        # Build messages list
        if messages:
            message_objs = _convert_messages(messages)
        else:
            message_objs: List[Any] = []
            if system_prompt:
                message_objs.append(system_message(system_prompt))
            if prompt:
                message_objs.append(user_message(prompt))

        total_chars = 0
        for m in message_objs:
            total_chars += len(m.content or "")
        
        print(f"Total input tokens for GrokLLM: {total_chars // 4}")

        # Build kwargs for chat.create
        kwargs: Dict[str, Any] = {
            "model": model,
            "temperature": (
                temperature if temperature is not None else Config.LLM_TEMPERATURE
            ),
            "max_tokens": (
                max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS
            ),
        }

        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if response_format:
            # Forwarded directly to underlying API (same field name)
            kwargs["response_format"] = response_format
        if reasoning_effort:
            # Only some Grok models support reasoning_effort; caller should pick a compatible one.
            kwargs["reasoning_effort"] = reasoning_effort

        chat = self.client.chat.create(**kwargs)

        for m in message_objs:
            chat.append(m)

        return chat

    def generate(
        self,
        prompt: str = "",
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
        model: str = Config.GROK_4_NON_REASONING_MODEL,
        stream: bool = False,
        tools: Optional[list] = None,
        tool_choice: str = None,
        messages: Optional[List[Any]] = None,
        return_raw: bool = False,
    ) -> Any:
        """
        Grok-backed equivalent of GroqLLM.generate.

        - If `stream=False` (default), returns a string (or raw response if return_raw=True).
        - If `stream=True`, returns the xai-sdk chat.stream() iterator.
        """

        max_attempts = 3

        for attempt in range(max_attempts):
            try:
                chat = self._build_chat(
                    model=model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    reasoning_effort=reasoning_effort,
                    tools=tools,
                    tool_choice=tool_choice,
                )

                if stream:
                    # For parity with GroqLLM, just return the stream iterator.
                    return chat.stream()

                response = chat.sample()

                if return_raw:
                    return response

                # xai-sdk chat responses expose `.content` as the text output.
                return (response.content or "").strip()

            except Exception as e:
                traceback.print_exc()
                if not _is_rate_limit_error(e):
                    logger.error(f"xAI Grok API error: {str(e)}")
                    raise RuntimeError(f"xAI Grok API error: {str(e)}")

                # Rate limit handling with exponential backoff: 60s, 120s, 240s
                if attempt == max_attempts - 1:
                    logger.error("Max retry attempts reached for Grok rate limit")
                    raise RuntimeError("xAI Grok API rate limit exceeded after retries")

                wait_time = 60 * (2**attempt)
                logger.info(
                    f"Rate limit hit for Grok, waiting {wait_time} seconds "
                    f"before retry {attempt + 1}/{max_attempts}"
                )
                time.sleep(wait_time)

    def count_tokens(self, text: str) -> int:
        tokens = self.client.tokenize.tokenize_text(
            model=Config.GROK_4_NON_REASONING_MODEL, text=text
        )

        return len(tokens)

    async def generate_async(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
        model: str = Config.GROK_4_NON_REASONING_MODEL,
    ) -> str:
        """
        Async equivalent using AsyncClient + chat.sample().
        (Note: no streaming flag here, to mirror your existing GroqLLM.generate_async.)
        """
        max_attempts = 3

        for attempt in range(max_attempts):
            try:
                # Build messages in the same way as sync path
                message_objs: List[Any] = []
                if system_prompt:
                    message_objs.append(system_message(system_prompt))
                if prompt:
                    message_objs.append(user_message(prompt))

                kwargs: Dict[str, Any] = {
                    "model": model,
                    "temperature": (
                        temperature
                        if temperature is not None
                        else Config.LLM_TEMPERATURE
                    ),
                    "max_tokens": (
                        max_tokens
                        if max_tokens is not None
                        else Config.LLM_MAX_TOKENS
                    ),
                }

                if response_format:
                    kwargs["response_format"] = response_format
                if reasoning_effort:
                    kwargs["reasoning_effort"] = reasoning_effort

                chat = self.async_client.chat.create(**kwargs)

                for m in message_objs:
                    chat.append(m)

                response = await chat.sample()
                return (response.content or "").strip()

            except Exception as e:
                if not _is_rate_limit_error(e):
                    logger.error(f"xAI Grok API error (async): {str(e)}")
                    raise RuntimeError(f"xAI Grok API error: {str(e)}")

                if attempt == max_attempts - 1:
                    logger.error(
                        "Max retry attempts reached for Grok rate limit (async)"
                    )
                    raise RuntimeError("xAI Grok API rate limit exceeded after retries")

                wait_time = 60 * (2**attempt)
                logger.info(
                    f"Rate limit hit for Grok (async), waiting {wait_time} seconds "
                    f"before retry {attempt + 1}/{max_attempts}"
                )
                await asyncio.sleep(wait_time)

    