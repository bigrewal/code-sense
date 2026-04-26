import asyncio
import logging
import time
from typing import Any

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
    if isinstance(exc, grpc.RpcError) and exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return True
    aio = getattr(grpc, "aio", None)
    if aio is not None and isinstance(exc, aio.AioRpcError) and exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in ("429", "rate limit", "resource_exhausted"))


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
        return "".join(
            part["text"]
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            else str(part)
            for part in content
            if not isinstance(part, dict) or isinstance(part.get("text"), str)
        )
    return str(content)


def _convert_messages(messages: list[Any]) -> list[Any]:
    converted: list[Any] = []
    factories = {"system": system_message, "user": user_message, "assistant": assistant_message}
    for msg in messages:
        if not isinstance(msg, dict):
            converted.append(msg)
            continue
        role = msg.get("role")
        if role not in factories:
            raise ValueError(f"Unsupported message role for xai-sdk: {role!r}")
        converted.append(factories[role](_normalise_content(msg.get("content", ""))))
    return converted


def _build_message_objects(
    *,
    prompt: str,
    system_prompt: str,
    messages: list[Any] | None = None,
) -> list[Any]:
    if messages:
        return _convert_messages(messages)
    return [
        message
        for message in (
            system_message(system_prompt) if system_prompt else None,
            user_message(prompt) if prompt else None,
        )
        if message
    ]


def _chat_kwargs(
    *,
    model: str,
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict[str, Any] | None,
    reasoning_effort: str | None,
    tools: list | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": temperature if temperature is not None else Config.LLM_TEMPERATURE,
        "max_tokens": max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS,
    }
    kwargs.update(
        {
            key: value
            for key, value in {
                "tools": tools,
                "tool_choice": tool_choice,
                "response_format": response_format,
                "reasoning_effort": reasoning_effort,
            }.items()
            if value
        }
    )
    return kwargs


class GrokLLM:
    def __init__(self):
        client_kwargs: dict[str, Any] = {}
        if hasattr(Config, "XAI_API_KEY"):
            client_kwargs["api_key"] = Config.XAI_API_KEY
        self.client = Client(**client_kwargs)
        self.async_client = AsyncClient(**client_kwargs)

    def _handle_retry(self, exc: Exception, attempt: int, *, async_mode: bool = False) -> int:
        if not _is_rate_limit_error(exc):
            _raise_api_error(exc, async_mode=async_mode)
        if attempt == MAX_ATTEMPTS - 1:
            raise _rate_limit_exhausted(async_mode=async_mode) from exc
        wait_time = _retry_delay(attempt)
        mode = " (async)" if async_mode else ""
        logger.info(
            "Rate limit hit for Grok{}, waiting {} seconds before retry {}/{}",
            mode,
            wait_time,
            attempt + 1,
            MAX_ATTEMPTS,
        )
        return wait_time

    def _build_chat(
        self,
        model: str,
        prompt: str,
        system_prompt: str,
        messages: list[Any] | None,
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        reasoning_effort: str | None,
        tools: list | None,
        tool_choice: str | None,
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
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        model: str = Config.GROK_4_NON_REASONING_MODEL,
        stream: bool = False,
        tools: list | None = None,
        tool_choice: str = None,
        messages: list[Any] | None = None,
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
                time.sleep(self._handle_retry(exc, attempt))

    def count_tokens(self, text: str) -> int:
        for attempt in range(MAX_ATTEMPTS):
            try:
                tokens = self.client.tokenize.tokenize_text(model=Config.GROK_4_NON_REASONING_MODEL, text=text)
                return len(tokens)
            except Exception as exc:
                time.sleep(self._handle_retry(exc, attempt))

    async def generate_async(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
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
                await asyncio.sleep(self._handle_retry(exc, attempt, async_mode=True))
