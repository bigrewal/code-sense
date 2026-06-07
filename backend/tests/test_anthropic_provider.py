import pytest

from app.llm.anthropic_provider import AnthropicProvider, _normalize_prompt_cache_ttl
from app.llm.base import LLMProviderError


def _provider(prompt_cache_ttl: str | None = "5m") -> AnthropicProvider:
    provider = object.__new__(AnthropicProvider)
    provider._prompt_cache_ttl = _normalize_prompt_cache_ttl(prompt_cache_ttl)
    return provider


def test_request_kwargs_marks_system_prompt_with_explicit_cache_control():
    kwargs = _provider()._request_kwargs(
        model="claude-sonnet-4-5",
        temperature=0.0,
        max_tokens=256,
        prompt="What changed?",
        system_prompt="Stable repository context",
    )

    assert "cache_control" not in kwargs
    assert kwargs["system"] == [
        {
            "type": "text",
            "text": "Stable repository context",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert kwargs["messages"] == [{"role": "user", "content": "What changed?"}]


def test_request_kwargs_supports_one_hour_prompt_cache_ttl():
    kwargs = _provider("1h")._request_kwargs(
        model="claude-sonnet-4-5",
        temperature=0.0,
        max_tokens=256,
        prompt="What changed?",
        system_prompt="Stable repository context",
    )

    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_request_kwargs_keeps_plain_system_prompt_when_cache_disabled():
    kwargs = _provider("false")._request_kwargs(
        model="claude-sonnet-4-5",
        temperature=0.0,
        max_tokens=256,
        prompt="What changed?",
        system_prompt="Stable repository context",
    )

    assert kwargs["system"] == "Stable repository context"


def test_request_kwargs_omits_empty_system_prompt():
    kwargs = _provider()._request_kwargs(
        model="claude-sonnet-4-5",
        temperature=0.0,
        max_tokens=256,
        prompt="What changed?",
        system_prompt="",
    )

    assert "system" not in kwargs


def test_prompt_cache_ttl_rejects_unknown_values():
    with pytest.raises(LLMProviderError):
        _normalize_prompt_cache_ttl("24h")
