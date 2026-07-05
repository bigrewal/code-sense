import json

import pytest

from app import chat_service


class FakeLLM:
    def __init__(self):
        self.repo_stream_calls = 0
        self.synthesis_stream_calls = 0

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def generate(self, prompt, system_prompt="", **kwargs):
        if kwargs.get("response_format") is chat_service.FileSelectionResponse:
            return '{"files_to_fetch":[]}'
        return "summary answer"

    async def generate_stream(self, prompt, system_prompt="", **kwargs):
        if "expert code repository analyst" in system_prompt:
            self.repo_stream_calls += 1
            yield "summary "
            yield "answer"
            return
        self.synthesis_stream_calls += 1
        yield "final answer"


class CapturingLLM(FakeLLM):
    def __init__(self):
        super().__init__()
        self.system_prompts = []

    async def generate(self, prompt, system_prompt="", **kwargs):
        self.system_prompts.append(system_prompt)
        return await super().generate(prompt, system_prompt, **kwargs)


class FileSelectingLLM(FakeLLM):
    async def generate(self, prompt, system_prompt="", **kwargs):
        if kwargs.get("response_format") is chat_service.FileSelectionResponse:
            return (
                '{"files_to_fetch":[{"file_path":"app.py",'
                '"info_needed":"implementation details"}]}'
            )
        if system_prompt == "Your task is to only fetch the information requested from the provided code":
            return "file insight"
        return "summary answer"


class FakeSubdirBriefDB:
    def list_brief_file_overviews_for_subdir(self, repo_name, subdir_path):
        assert repo_name == "repo-a"
        assert subdir_path == "backend/app"
        return [
            {
                "file_path": "backend/app/main.py",
                "data": "`backend/app/main.py` defines the FastAPI chat endpoint.",
            }
        ]


class EmptySubdirBriefDB:
    def list_brief_file_overviews_for_subdir(self, repo_name, subdir_path):
        return []


@pytest.mark.asyncio
async def test_stateless_chat_stream_emits_error_event(monkeypatch):
    async def _boom(**_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(chat_service, "get_rephrased_question", _boom)

    events = [json.loads(line) async for line in chat_service.stateless_stream_chat("repo-a", "hello")]

    assert events[0]["type"] == "progress"
    assert events[-1]["type"] == "error"
    assert events[-1]["message"] == "Chat failed. Please try again."


@pytest.mark.asyncio
async def test_stream_answer_uses_provider_generate(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(chat_service, "_llm", llm)

    async def _repo_context(_repo_name):
        return "`app.py` summary"

    monkeypatch.setattr(chat_service, "_get_cached_repo_context", _repo_context)

    events = [event async for event in chat_service.stream_answer("How does it work?", "repo-a")]

    assert any(
        event["type"] == "progress"
        and event["stage"] == "selecting_files"
        and event["status"] == "completed"
        for event in events
    )
    content_deltas = [event["delta"] for event in events if event["type"] == "content"]
    assert content_deltas == ["summary ", "answer", "\n"]
    assert llm.repo_stream_calls == 1
    assert llm.synthesis_stream_calls == 0


@pytest.mark.asyncio
async def test_stream_answer_synthesizes_when_file_insights_exist(monkeypatch):
    llm = FileSelectingLLM()
    monkeypatch.setattr(chat_service, "_llm", llm)
    monkeypatch.setattr(chat_service, "fetch_code_file", lambda **_kwargs: "print('hello')")

    async def _repo_context(_repo_name):
        return "`app.py` summary"

    monkeypatch.setattr(chat_service, "_get_cached_repo_context", _repo_context)

    events = [event async for event in chat_service.stream_answer("How does it work?", "repo-a")]

    assert {"type": "content", "delta": "final answer"} in events
    assert llm.repo_stream_calls == 1
    assert llm.synthesis_stream_calls == 1


@pytest.mark.asyncio
async def test_stream_answer_uses_only_subdir_briefs_when_requested(monkeypatch):
    llm = CapturingLLM()
    monkeypatch.setattr(chat_service, "_llm", llm)
    monkeypatch.setattr(chat_service, "get_db_client", lambda: FakeSubdirBriefDB())

    async def _repo_context(_repo_name):
        return "`README.md` describes the project."

    monkeypatch.setattr(chat_service, "_get_cached_repo_context", _repo_context)

    events = [
        event
        async for event in chat_service.stream_answer("How does @backend/app work?", "repo-a")
    ]

    loading_events = [
        event
        for event in events
        if event["type"] == "progress" and event["stage"] == "loading_subdir_context"
    ]
    assert loading_events[-1]["status"] == "completed"
    assert loading_events[-1]["metadata"]["file_count"] == 1
    prompts = "\n\n".join(llm.system_prompts)
    assert "SUBDIRECTORY @backend/app FILE BRIEFS (1 files):" in prompts
    assert "`backend/app/main.py` defines the FastAPI chat endpoint." in prompts
    assert "`README.md` describes the project." not in prompts


@pytest.mark.asyncio
async def test_stream_answer_falls_back_to_full_context_when_subdir_is_missing(monkeypatch):
    llm = CapturingLLM()
    monkeypatch.setattr(chat_service, "_llm", llm)
    monkeypatch.setattr(chat_service, "get_db_client", lambda: EmptySubdirBriefDB())

    async def _repo_context(_repo_name):
        return "`README.md` describes the project."

    monkeypatch.setattr(chat_service, "_get_cached_repo_context", _repo_context)

    events = [
        event
        async for event in chat_service.stream_answer("How does @missing/path work?", "repo-a")
    ]

    loading_events = [
        event
        for event in events
        if event["type"] == "progress" and event["stage"] == "loading_subdir_context"
    ]
    assert loading_events[-1]["status"] == "failed"
    assert loading_events[-1]["metadata"]["missing"] == ["missing/path"]
    assert "`README.md` describes the project." in "\n\n".join(llm.system_prompts)
