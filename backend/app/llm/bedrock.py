from __future__ import annotations

import logging
from typing import Any

from .anthropic_provider import AnthropicProvider, _normalize_prompt_cache_ttl
from .base import LLMProviderError

logger = logging.getLogger(__name__)


class BedrockProvider(AnthropicProvider):
    """Claude on AWS Bedrock via the anthropic SDK's AnthropicBedrock client.

    Inherits the message/structured-output/streaming logic from AnthropicProvider —
    the only difference is the underlying client, which authenticates with AWS
    credentials and uses Bedrock model IDs (e.g. anthropic.claude-sonnet-4-5-v1:0).
    """

    name = "bedrock"
    DEFAULT_MODEL = "anthropic.claude-sonnet-4-5-v1:0"

    def __init__(
        self,
        *,
        aws_region: str | None = None,
        aws_access_key: str | None = None,
        aws_secret_key: str | None = None,
        aws_session_token: str | None = None,
        default_model: str | None = None,
        prompt_cache_ttl: str | None = "5m",
    ):
        client_kwargs: dict[str, Any] = {}
        if aws_region:
            client_kwargs["aws_region"] = aws_region
        if aws_access_key:
            client_kwargs["aws_access_key"] = aws_access_key
        if aws_secret_key:
            client_kwargs["aws_secret_key"] = aws_secret_key
        if aws_session_token:
            client_kwargs["aws_session_token"] = aws_session_token

        try:
            from anthropic import AnthropicBedrock, AsyncAnthropicBedrock  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMProviderError(
                "anthropic[bedrock] not installed. Install with: uv sync --extra bedrock"
            ) from exc

        self._async_client = AsyncAnthropicBedrock(**client_kwargs)
        self._sync_client = AnthropicBedrock(**client_kwargs)
        self._default_model = default_model or self.DEFAULT_MODEL
        self._prompt_cache_ttl = _normalize_prompt_cache_ttl(prompt_cache_ttl)
        from .openai_provider import _load_tiktoken_encoder
        self._encoder = _load_tiktoken_encoder()

    def _build_clients(self, kwargs: dict[str, Any]):
        # Not used — __init__ constructs Bedrock clients directly. Kept to satisfy
        # the parent contract in case it's ever called.
        raise NotImplementedError("BedrockProvider builds its own clients in __init__")
