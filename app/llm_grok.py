import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import grpc
from xai_sdk import Client, AsyncClient
from xai_sdk.chat import assistant as assistant_message
from xai_sdk.chat import system as system_message
from xai_sdk.chat import user as user_message

from .config import Config

logger = logging.getLogger(__name__)
logging.getLogger("grpc").setLevel(logging.WARNING)

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 60


def _is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, grpc.RpcError):
        if exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
            return True

    aio = getattr(grpc, "aio", None)
    if aio is not None and isinstance(exc, aio.AioRpcError) and exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return True

    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "resource_exhausted" in msg


def _raise_api_error(exc: Exception, *, async_mode: bool = False) -> None:
    mode = " (async)" if async_mode else ""
    logger.error("xAI Grok API error{}: {}", mode, exc)
    raise RuntimeError(f"xAI Grok API error: {exc}") from exc


def _retry_delay(attempt: int) -> int:
    return BASE_BACKOFF_SECONDS * (2**attempt)


def _rate_limit_exhausted(async_mode: bool = False) -> RuntimeError:
    mode = " (async)" if async_mode else ""
    logger.error("Max retry attempts reached for Grok rate limit{}", mode)
    return RuntimeError("xAI Grok API rate limit exceeded after retries")


def _normalise_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _convert_messages(messages: List[Any]) -> List[Any]:
    converted: List[Any] = []
    for msg in messages:
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


def _build_message_objects(
    *,
    prompt: str,
    system_prompt: str,
    messages: Optional[List[Any]] = None,
) -> List[Any]:
    if messages:
        return _convert_messages(messages)
    message_objs: List[Any] = []
    if system_prompt:
        message_objs.append(system_message(system_prompt))
    if prompt:
        message_objs.append(user_message(prompt))
    return message_objs


def _chat_kwargs(
    *,
    model: str,
    temperature: Optional[float],
    max_tokens: Optional[int],
    response_format: Optional[Dict[str, Any]],
    reasoning_effort: Optional[str],
    tools: Optional[list] = None,
    tool_choice: Optional[str] = None,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "model": model,
        "temperature": temperature if temperature is not None else Config.LLM_TEMPERATURE,
        "max_tokens": max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS,
    }
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    if response_format:
        kwargs["response_format"] = response_format
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


class GrokLLM:
    def __init__(self):
        client_kwargs: Dict[str, Any] = {}
        if hasattr(Config, "XAI_API_KEY"):
            client_kwargs["api_key"] = Config.XAI_API_KEY
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
        message_objs = _build_message_objects(prompt=prompt, system_prompt=system_prompt, messages=messages)
        logger.debug("Total input tokens for GrokLLM: {}", sum(len(m.content or "") for m in message_objs) // 4)
        chat = self.client.chat.create(
            **_chat_kwargs(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
                tools=tools,
                tool_choice=tool_choice,
            )
        )
        for msg in message_objs:
            chat.append(msg)
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
        for attempt in range(MAX_ATTEMPTS):
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
                    return chat.stream()
                response = chat.sample()
                return response if return_raw else (response.content or "").strip()
            except Exception as exc:
                if not _is_rate_limit_error(exc):
                    _raise_api_error(exc)
                if attempt == MAX_ATTEMPTS - 1:
                    raise _rate_limit_exhausted() from exc
                wait_time = _retry_delay(attempt)
                logger.info("Rate limit hit for Grok, waiting {} seconds before retry {}/{}", wait_time, attempt + 1, MAX_ATTEMPTS)
                time.sleep(wait_time)

    def count_tokens(self, text: str) -> int:
        for attempt in range(MAX_ATTEMPTS):
            try:
                tokens = self.client.tokenize.tokenize_text(model=Config.GROK_4_NON_REASONING_MODEL, text=text)
                return len(tokens)
            except Exception as exc:
                if not _is_rate_limit_error(exc):
                    _raise_api_error(exc)
                if attempt == MAX_ATTEMPTS - 1:
                    raise _rate_limit_exhausted() from exc
                wait_time = _retry_delay(attempt)
                logger.info("Rate limit hit for Grok, waiting {} seconds before retry {}/{}", wait_time, attempt + 1, MAX_ATTEMPTS)
                time.sleep(wait_time)

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
        for attempt in range(MAX_ATTEMPTS):
            try:
                message_objs = _build_message_objects(prompt=prompt, system_prompt=system_prompt)
                chat = self.async_client.chat.create(
                    **_chat_kwargs(
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                        reasoning_effort=reasoning_effort,
                    )
                )
                for msg in message_objs:
                    chat.append(msg)
                response = await chat.sample()
                return (response.content or "").strip()
            except Exception as exc:
                if not _is_rate_limit_error(exc):
                    _raise_api_error(exc, async_mode=True)
                if attempt == MAX_ATTEMPTS - 1:
                    raise _rate_limit_exhausted(async_mode=True) from exc
                wait_time = _retry_delay(attempt)
                logger.info(
                    "Rate limit hit for Grok (async), waiting {} seconds before retry {}/{}",
                    wait_time,
                    attempt + 1,
                    MAX_ATTEMPTS,
                )
                await asyncio.sleep(wait_time)
