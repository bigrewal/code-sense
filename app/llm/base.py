from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator, Type

from pydantic import BaseModel

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 60


def retry_delay(attempt: int) -> int:
    return BASE_BACKOFF_SECONDS * (2**attempt)


class LLMProviderError(RuntimeError):
    """Raised when an LLM provider call fails after retries."""


class LLMRateLimitError(LLMProviderError):
    """Raised when retries are exhausted due to rate limiting."""


ResponseFormat = Type[BaseModel] | None


class LLMProvider(ABC):
    """Provider-agnostic interface for the small subset of LLM features CodeSense uses."""

    name: str = "abstract"

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Count tokens in the given text. Synchronous; called from worker threads."""

    @abstractmethod
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
        """Return a single response string. If `response_format` is given, the response
        is JSON conforming to that Pydantic schema; callers parse with model_validate_json."""

    @abstractmethod
    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas as they arrive."""
