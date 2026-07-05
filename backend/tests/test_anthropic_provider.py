import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.llm.anthropic_provider import (
    AnthropicProvider,
    _normalize_prompt_cache_ttl,
    _normalize_structured_output,
)
from app.llm.base import LLMProviderError


class FileSelection(BaseModel):
    file_path: str
    info_needed: str


class FileSelectionResponse(BaseModel):
    files_to_fetch: list[FileSelection]


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
    assert kwargs["temperature"] == 0.0


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5",
        "claude-sonnet-5-20260630",
        "anthropic.claude-sonnet-5-v1:0",
        "claude-opus-4-7",
        "claude-opus-4-8",
    ],
)
def test_request_kwargs_omits_temperature_for_models_without_sampling_controls(model: str):
    kwargs = _provider()._request_kwargs(
        model=model,
        temperature=0.0,
        max_tokens=256,
        prompt="What changed?",
        system_prompt="Stable repository context",
    )

    assert "temperature" not in kwargs


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


@pytest.mark.parametrize(
    "value",
    [
        {"files_to_fetch": {"files_to_fetch": []}},
        {"unexpected": "value"},
    ],
)
def test_structured_output_rejects_irreparable_values(value):
    with pytest.raises(LLMProviderError, match="did not match FileSelectionResponse"):
        _normalize_structured_output(value, FileSelectionResponse)


@pytest.mark.asyncio
async def test_generate_structured_repairs_anthropic_double_encoded_tool_input():
    provider = _provider()
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                input={"files_to_fetch": '{"files_to_fetch":[]}'},
            )
        ]
    )

    class Messages:
        async def create(self, **_kwargs):
            return response

    provider._async_client = SimpleNamespace(messages=Messages())
    provider._default_model = "claude-sonnet-5"

    result = await provider.generate(
        "Select files",
        response_format=FileSelectionResponse,
    )

    assert json.loads(result) == {"files_to_fetch": []}
