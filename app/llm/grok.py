from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import grpc
from xai_sdk import AsyncClient, Client
from xai_sdk.chat import system as system_message
from xai_sdk.chat import user as user_message

from ..config import Config
from .base import (
    MAX_ATTEMPTS,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    ResponseFormat,
    retry_delay,
)

logger = logging.getLogger(__name__)
logging.getLogger("grpc").setLevel(logging.WARNING)


def _is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, grpc.RpcError) and exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return True
    aio = getattr(grpc, "aio", None)
    if aio is not None and isinstance(exc, aio.AioRpcError) and exc.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in ("429", "rate limit", "resource_exhausted"))


class GrokProvider(LLMProvider):
    name = "grok"

    def __init__(self, *, api_key: str | None = None, default_model: str | None = None):
        client_kwargs: dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        self._client = Client(**client_kwargs)
        self._async_client = AsyncClient(**client_kwargs)
        self._default_model = default_model or Config.GROK_4_NON_REASONING_MODEL

    def _resolve_model(self, model: str | None) -> str:
        return model or self._default_model

    def _common_kwargs(
        self,
        *,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        response_format: ResponseFormat,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature if temperature is not None else Config.LLM_TEMPERATURE,
            "max_tokens": max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        return kwargs

    def _handle_failure(self, exc: Exception, attempt: int, *, async_mode: bool) -> int:
        if not _is_rate_limit_error(exc):
            logger.error("xAI Grok API error%s: %s", " (async)" if async_mode else "", exc)
            raise LLMProviderError(f"xAI Grok API error: {exc}") from exc
        if attempt == MAX_ATTEMPTS - 1:
            logger.error("Max retry attempts reached for Grok rate limit%s", " (async)" if async_mode else "")
            raise LLMRateLimitError("xAI Grok API rate limit exceeded after retries") from exc
        wait = retry_delay(attempt)
        logger.info("Grok rate-limited, retry %d/%d in %ds", attempt + 1, MAX_ATTEMPTS, wait)
        return wait

    def count_tokens(self, text: str) -> int:
        for attempt in range(MAX_ATTEMPTS):
            try:
                tokens = self._client.tokenize.tokenize_text(model=self._default_model, text=text)
                return len(tokens)
            except Exception as exc:
                import time
                time.sleep(self._handle_failure(exc, attempt, async_mode=False))
        raise LLMRateLimitError("xAI Grok API rate limit exceeded after retries")

    def _build_messages(self, prompt: str, system_prompt: str) -> list[Any]:
        return [m for m in (
            system_message(system_prompt) if system_prompt else None,
            user_message(prompt) if prompt else None,
        ) if m]

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: ResponseFormat = None,
        model: str | None = None,
    ) -> str:
        model = self._resolve_model(model)
        for attempt in range(MAX_ATTEMPTS):
            try:
                chat = self._async_client.chat.create(
                    **self._common_kwargs(
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                    )
                )
                for msg in self._build_messages(prompt, system_prompt):
                    chat.append(msg)
                response = await chat.sample()
                return (response.content or "").strip()
            except Exception as exc:
                await asyncio.sleep(self._handle_failure(exc, attempt, async_mode=True))
        raise LLMRateLimitError("xAI Grok API rate limit exceeded after retries")

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        model = self._resolve_model(model)
        for attempt in range(MAX_ATTEMPTS):
            try:
                chat = self._async_client.chat.create(
                    **self._common_kwargs(
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=None,
                    )
                )
                for msg in self._build_messages(prompt, system_prompt):
                    chat.append(msg)
                async for _, chunk in chat.stream():
                    content = getattr(chunk, "content", None)
                    if content:
                        yield content
                return
            except Exception as exc:
                await asyncio.sleep(self._handle_failure(exc, attempt, async_mode=True))
        raise LLMRateLimitError("xAI Grok API rate limit exceeded after retries")
