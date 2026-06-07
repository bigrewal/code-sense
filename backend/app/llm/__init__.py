from __future__ import annotations

import logging

from ..config import Config
from .base import (
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
)

logger = logging.getLogger(__name__)


def get_llm_provider() -> LLMProvider:
    """Construct the LLM provider configured by Config.LLM_PROVIDER."""
    name = (Config.LLM_PROVIDER or "grok").lower()
    model = Config.LLM_MODEL or None

    if name == "grok":
        from .grok import GrokProvider
        return GrokProvider(api_key=Config.XAI_API_KEY, default_model=model)

    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_BASE_URL or None,
            default_model=model,
        )

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(
            api_key=Config.ANTHROPIC_API_KEY,
            default_model=model,
            prompt_cache_ttl=Config.ANTHROPIC_PROMPT_CACHE_TTL,
        )

    if name == "bedrock":
        from .bedrock import BedrockProvider
        return BedrockProvider(
            aws_region=Config.AWS_REGION or None,
            aws_access_key=Config.AWS_ACCESS_KEY_ID or None,
            aws_secret_key=Config.AWS_SECRET_ACCESS_KEY or None,
            aws_session_token=Config.AWS_SESSION_TOKEN or None,
            default_model=model,
            prompt_cache_ttl=Config.ANTHROPIC_PROMPT_CACHE_TTL,
        )

    raise LLMProviderError(
        f"Unknown LLM_PROVIDER={name!r}. Supported: grok, openai, anthropic, bedrock."
    )


__all__ = [
    "LLMProvider",
    "LLMProviderError",
    "LLMRateLimitError",
    "get_llm_provider",
]
