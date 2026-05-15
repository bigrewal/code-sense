from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

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


def _is_rate_limit_error(exc: Exception) -> bool:
    try:
        from openai import RateLimitError  # type: ignore[import-not-found]
        if isinstance(exc, RateLimitError):
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg or "too many requests" in msg


class OpenAIProvider(LLMProvider):
    name = "openai"
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ):
        try:
            from openai import AsyncOpenAI, OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMProviderError(
                "openai package not installed. Install with: uv sync --extra openai"
            ) from exc

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self._async_client = AsyncOpenAI(**kwargs)
        self._default_model = default_model or self.DEFAULT_MODEL
        self._encoder = _load_tiktoken_encoder()

    def _resolve_model(self, model: str | None) -> str:
        return model or self._default_model

    def _handle_failure(self, exc: Exception, attempt: int) -> int:
        if not _is_rate_limit_error(exc):
            logger.error("OpenAI API error: %s", exc)
            raise LLMProviderError(f"OpenAI API error: {exc}") from exc
        if attempt == MAX_ATTEMPTS - 1:
            logger.error("Max retry attempts reached for OpenAI rate limit")
            raise LLMRateLimitError("OpenAI rate limit exceeded after retries") from exc
        wait = retry_delay(attempt)
        logger.info("OpenAI rate-limited, retry %d/%d in %ds", attempt + 1, MAX_ATTEMPTS, wait)
        return wait

    def count_tokens(self, text: str) -> int:
        return _approx_token_count(self._encoder, text)

    def _messages(self, prompt: str, system_prompt: str) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        if prompt:
            msgs.append({"role": "user", "content": prompt})
        return msgs

    def _request_kwargs(
        self,
        *,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        prompt: str,
        system_prompt: str,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": self._messages(prompt, system_prompt),
            "temperature": temperature if temperature is not None else Config.LLM_TEMPERATURE,
            "max_tokens": max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS,
        }

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
                kwargs = self._request_kwargs(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                if response_format is not None:
                    completion = await self._async_client.beta.chat.completions.parse(
                        **kwargs,
                        response_format=response_format,
                    )
                    parsed = completion.choices[0].message.parsed
                    if parsed is None:
                        return (completion.choices[0].message.content or "").strip()
                    return parsed.model_dump_json()
                completion = await self._async_client.chat.completions.create(**kwargs)
                return (completion.choices[0].message.content or "").strip()
            except Exception as exc:
                await asyncio.sleep(self._handle_failure(exc, attempt))
        raise LLMRateLimitError("OpenAI rate limit exceeded after retries")

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
                kwargs = self._request_kwargs(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                stream = await self._async_client.chat.completions.create(**kwargs, stream=True)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
                return
            except Exception as exc:
                await asyncio.sleep(self._handle_failure(exc, attempt))
        raise LLMRateLimitError("OpenAI rate limit exceeded after retries")


def _load_tiktoken_encoder():
    try:
        import tiktoken  # type: ignore[import-not-found]
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def _approx_token_count(encoder, text: str) -> int:
    if encoder is not None:
        return len(encoder.encode(text))
    # Conservative fallback: ~4 chars per token.
    return max(1, len(text) // 4)
