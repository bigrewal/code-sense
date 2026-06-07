from __future__ import annotations

import asyncio
import json
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
from .openai_provider import _approx_token_count, _load_tiktoken_encoder

logger = logging.getLogger(__name__)


_PROMPT_CACHE_DISABLED_VALUES = {"", "0", "false", "off", "none", "disabled"}


def _is_rate_limit_error(exc: Exception) -> bool:
    try:
        from anthropic import RateLimitError  # type: ignore[import-not-found]
        if isinstance(exc, RateLimitError):
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    return "rate limit" in msg or "429" in msg or "overloaded" in msg


def _normalize_prompt_cache_ttl(ttl: str | None) -> str | None:
    normalized = (ttl or "5m").strip().lower()
    if normalized in _PROMPT_CACHE_DISABLED_VALUES:
        return None
    if normalized not in {"5m", "1h"}:
        raise LLMProviderError(
            "ANTHROPIC_PROMPT_CACHE_TTL must be one of: 5m, 1h, false"
        )
    return normalized


def _cache_control(ttl: str) -> dict[str, str]:
    control = {"type": "ephemeral"}
    if ttl == "1h":
        control["ttl"] = "1h"
    return control


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    DEFAULT_MODEL = "claude-sonnet-4-5"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str | None = None,
        prompt_cache_ttl: str | None = "5m",
    ):
        client_kwargs: dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        self._async_client, self._sync_client = self._build_clients(client_kwargs)
        self._default_model = default_model or self.DEFAULT_MODEL
        self._prompt_cache_ttl = _normalize_prompt_cache_ttl(prompt_cache_ttl)
        self._encoder = _load_tiktoken_encoder()

    def _build_clients(self, kwargs: dict[str, Any]):
        try:
            from anthropic import Anthropic, AsyncAnthropic  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMProviderError(
                "anthropic package not installed. Install with: uv sync --extra anthropic"
            ) from exc
        return AsyncAnthropic(**kwargs), Anthropic(**kwargs)

    def _resolve_model(self, model: str | None) -> str:
        return model or self._default_model

    def _resolve_max_tokens(self, max_tokens: int | None) -> int:
        # Anthropic requires max_tokens. Default to Config.LLM_MAX_TOKENS.
        return max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS

    def _resolve_temperature(self, temperature: float | None) -> float:
        return temperature if temperature is not None else Config.LLM_TEMPERATURE

    def _handle_failure(self, exc: Exception, attempt: int) -> int:
        if not _is_rate_limit_error(exc):
            logger.error("Anthropic API error: %s", exc)
            raise LLMProviderError(f"Anthropic API error: {exc}") from exc
        if attempt == MAX_ATTEMPTS - 1:
            logger.error("Max retry attempts reached for Anthropic rate limit")
            raise LLMRateLimitError("Anthropic rate limit exceeded after retries") from exc
        wait = retry_delay(attempt)
        logger.info("Anthropic rate-limited, retry %d/%d in %ds", attempt + 1, MAX_ATTEMPTS, wait)
        return wait

    def count_tokens(self, text: str) -> int:
        return _approx_token_count(self._encoder, text)

    def _system_prompt(self, system_prompt: str) -> str | list[dict[str, Any]]:
        if not self._prompt_cache_ttl:
            return system_prompt
        return [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": _cache_control(self._prompt_cache_ttl),
            }
        ]

    def _request_kwargs(
        self,
        *,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        prompt: str,
        system_prompt: str,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._resolve_max_tokens(max_tokens),
            "temperature": self._resolve_temperature(temperature),
            "messages": [{"role": "user", "content": prompt}] if prompt else [],
        }
        if system_prompt:
            kwargs["system"] = self._system_prompt(system_prompt)
        return kwargs

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
        if response_format is not None:
            return await self._generate_structured(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                model=model,
            )

        for attempt in range(MAX_ATTEMPTS):
            try:
                kwargs = self._request_kwargs(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                response = await self._async_client.messages.create(**kwargs)
                return _extract_text(response).strip()
            except Exception as exc:
                await asyncio.sleep(self._handle_failure(exc, attempt))
        raise LLMRateLimitError("Anthropic rate limit exceeded after retries")

    async def _generate_structured(
        self,
        *,
        prompt: str,
        system_prompt: str,
        temperature: float | None,
        max_tokens: int | None,
        response_format: ResponseFormat,
        model: str,
    ) -> str:
        # Anthropic doesn't have an OpenAI-style response_format; coerce JSON via a tool.
        schema = response_format.model_json_schema()
        tool_name = response_format.__name__
        tool = {
            "name": tool_name,
            "description": f"Return data conforming to the {tool_name} schema.",
            "input_schema": schema,
        }
        for attempt in range(MAX_ATTEMPTS):
            try:
                kwargs = self._request_kwargs(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
                kwargs["tools"] = [tool]
                kwargs["tool_choice"] = {"type": "tool", "name": tool_name}
                response = await self._async_client.messages.create(**kwargs)
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        return json.dumps(block.input)
                # Fallback: model returned text instead of using the tool.
                return _extract_text(response).strip()
            except Exception as exc:
                await asyncio.sleep(self._handle_failure(exc, attempt))
        raise LLMRateLimitError("Anthropic rate limit exceeded after retries")

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
                async with self._async_client.messages.stream(**kwargs) as stream:
                    async for delta in stream.text_stream:
                        if delta:
                            yield delta
                return
            except Exception as exc:
                await asyncio.sleep(self._handle_failure(exc, attempt))
        raise LLMRateLimitError("Anthropic rate limit exceeded after retries")


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)
