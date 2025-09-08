"""
Repo Walkthrough Workflow (repo-, dir-, and file-level)

A modular, production-ready skeleton for explaining a repository end-to-end by
walking entry points -> dependencies -> deeper levels, streaming LLM output.

Assumptions per user's environment:
- Tools are already implemented and imported from `.tools` (see imports below).
- An LLM client instance `llm` is provided with a `.generate()` method.
  - Use `llm.generate(prompt=..., stream=True)` for streaming to the caller.
- No chunking: the entire file content is sent to the LLM in one request.
- FastAPI-friendly: expose generators so `StreamingResponse` can consume them.

Now supports:
- Repo-level walkthrough (entry points first)
- Dir-level walkthrough (all files returned by `list_files_in_dir`)
- File-level walkthrough (single-path focus)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Callable, Sequence, Set, List, Any, Iterator, Optional
import textwrap

# ---- Provided tools (imported directly; no separate CodeTools protocol) ----
from .tools.fetch_entry_point_files import fetch_entry_point_files_as_list
from .tools.fetch_cross_file_refs_for_file import fetch_cross_file_refs_for_file_as_list
from .tools.fetch_code_file import fetch_code_file

from .db import get_mongo_client

# Optional import for dir listing (ask the user to provide if unavailable)
try:  # pragma: no cover - availability depends on host project
    from .tools.list_files_in_dir import list_files_in_dir  # type: ignore
except Exception:  # pragma: no cover
    list_files_in_dir = None  # type: ignore

# ------------------------------
# Small helpers
# ------------------------------

EXTS: Set[str] = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
    ".scala", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".m", ".mm",
    ".swift", ".sql", ".sh", ".bash", ".ps1", ".yaml", ".yml", ".json",
}


def looks_like_code_path(path: str) -> bool:
    path = path.strip().split("?", 1)[0]
    return any(path.endswith(ext) for ext in EXTS)


@dataclass
class WalkItem:
    path: str
    depth: int


@dataclass
class WalkConfig:
    max_depth: int = 3
    max_files: int = 200


# ------------------------------
# Prompting (single combined prompt string)
# ------------------------------

class PromptBuilder:
    SYSTEM_RULES = textwrap.dedent(
        """
        You are a senior codebase explainer. Be precise, terse, and correct.
        Rules:
        - Never invent APIs or behavior. If unknown, say "Unknown".
        - Prefer bullet points over paragraphs. Use short, information-dense lines.
        - Always relate the current snippet back to the user's question and repo goals.
        - When helpful, name important types/functions exactly as in code.
        Output must be valid Markdown, no code fences unless showing code.
        """
    ).strip()

    FILE_TMPL = textwrap.dedent(
        """
        [SYSTEM RULES]
{rules}

        [SEED CONTEXT]
{seed}

        [USER QUESTION]
{question}

        [GLOBAL OUTLINE SO FAR]
{outline}

        [CURRENT FILE]
{path}

        [CODE]
```
{code}
```

        Write a concise explanation with these sections (only if applicable):
        - Role: 1–3 bullets on what this file is responsible for.
        - Key APIs: list important exported functions/classes/types.
        - Flow: how control/data move; which functions call which.
        - External deps: list notable libraries/services.
        - Cross-file links: other files/modules that this file clearly relies on (paths or module names).
        - Gotchas: edge cases, invariants, error handling.

        Keep each bullet very short. If something is unclear, say "Unknown".
        """
    ).strip()

    OUTLINE_TMPL = textwrap.dedent(
        """
        [SYSTEM RULES]
{rules}

        You are updating a global outline for an end-to-end walkthrough.

        Current outline:
        {outline}

        New file summary (file: {path}):
        {file_summary}

        Produce a revised outline with at most 12 top-level bullets capturing
        the system's flow end-to-end. Keep bullets short. Merge/rename existing
        bullets; do not duplicate. If the new info is local-only, add a nested sub-bullet.
        Output: Markdown bullet list only.
        """
    ).strip()

    def __init__(self, seed: str, question: str):
        self.seed = seed.strip() or "(none)"
        self.question = question.strip()

    def file_prompt(self, *, outline: str, path: str, code: str) -> str:
        return self.FILE_TMPL.format(
            rules=self.SYSTEM_RULES,
            seed=self.seed,
            question=self.question,
            outline=outline or "(empty)",
            path=path,
            code=code,
        )

    def outline_prompt(self, *, outline: str, path: str, file_summary: str) -> str:
        return self.OUTLINE_TMPL.format(
            rules=self.SYSTEM_RULES,
            outline=outline or "(empty)",
            path=path,
            file_summary=file_summary,
        )


# ------------------------------
# Streaming helpers
# ------------------------------

StreamFn = Callable[[str], None]


def default_streamer(chunk: str) -> None:
    print(chunk, flush=True)


# ------------------------------
# Base workflow (shared core with custom initial paths)
# ------------------------------

@dataclass
class _BaseWalkthrough:
    llm: Any  # Provided client exposing .generate(prompt=..., stream=bool)
    repo_name: str
    seed_prompt: str
    user_question: str
    config: WalkConfig = field(default_factory=WalkConfig)

    mongo_client = get_mongo_client()
    mental_model_collection = mongo_client["mental_model"]

    # live state
    _outline: str = ""
    _visited: Set[str] = field(default_factory=set)

    # ---- LLM adapters ----
    def _llm_generate_stream(self, prompt: str) -> Iterator[str]:
        """Yield chunks from the model using stream=True; normalize to strings.
        Compatible with Groq-like payloads exposing choices[0].delta.content.
        """
        result = self.llm.generate(prompt=prompt, stream=True, temperature=0.0, reasoning_effort="low")
        # If the client ignored streaming
        if isinstance(result, str):
            yield result
            return
        try:
            for chunk in result: 
                content = chunk.choices[0].delta.content or ""
                if content: 
                    yield content
        except TypeError:
            yield str(result)

    def _llm_text(self, prompt: str) -> str:
        """Non-streaming convenience for outline updates."""
        resp = self.llm.generate(prompt=prompt, stream=False)
        if isinstance(resp, str):
            return resp
        try:
            # If resp is an iterator/generator, join its items
            return "".join(str(x) for x in resp)
        except TypeError:
            return str(resp)

    # ---- Core walk ----
    def _walk(self, initial_paths: Sequence[str]) -> Iterator[str]:
        builder = PromptBuilder(seed=self.seed_prompt, question=self.user_question)

        queue: deque[WalkItem] = deque(WalkItem(p, 0) for p in initial_paths if looks_like_code_path(p))

        if not queue:
            yield "**No code files found to explain.**\n"
            return

        files_processed = 0
        yield f"# Walkthrough for `{self.repo_name}`\n"
        yield "_Progressive walkthrough starting from the requested scope and expanding through dependencies._\n"

        while queue and files_processed < self.config.max_files:
            item = queue.popleft()
            if item.depth > self.config.max_depth:
                continue
            if item.path in self._visited:
                continue
            self._visited.add(item.path)

            # Fetch and explain this file (no chunking)
            try:
                code = fetch_code_file(item.path)
            except Exception as e:
                yield f"\n## {item.path}\nFailed to fetch code: {e}\n"
                continue

            yield f"\n## {item.path}\n"

            parts: List[str] = []
            # Check mongoDB
            existing_insight = self.mental_model_collection.find_one({
                "repo_id": self.repo_name,
                "file_path": item.path,
                "document_type": "FILE_SUMMARY"
            })

            if existing_insight:
                summary = existing_insight["data"]
                parts.append(summary)
                yield summary + "\n"
            
            else:
                file_prompt = builder.file_prompt(outline=self._outline, path=item.path, code=code)

                # Stream model output to caller while collecting for outline update
                
                try:
                    for piece in self._llm_generate_stream(file_prompt):
                        parts.append(piece)
                        yield piece
                except Exception as e:
                    err = f"- Error generating summary: {e}\n"
                    parts.append(err)
                    yield err

                file_summary = "".join(parts)
                document = {
                    "repo_id": self.repo_name,
                    "file_path": item.path,
                    "document_type": "FILE_SUMMARY",
                    "data": file_summary
                }
                self.mental_model_collection.update_one(
                    {"repo_id": self.repo_name, "file_path": item.path, "document_type": "FILE_SUMMARY"},
                    {"$set": document},
                    upsert=True
                )

            # Update global outline using the per-file summary
            try:
                outline_prompt = builder.outline_prompt(outline=self._outline, path=item.path, file_summary=file_summary)
                self._outline = self._llm_text(outline_prompt)
            except Exception as e:
                yield f"\n> Outline update failed: {e}\n"

            # Discover and enqueue dependencies
            try:
                _, deps = fetch_cross_file_refs_for_file_as_list(item.path, self.repo_name)
            except Exception as e:
                deps = []
                yield f"\n> Dependency lookup failed for {item.path}: {e}\n"

            next_paths = [d for d in deps if looks_like_code_path(d) and d not in self._visited]
            for d in next_paths:
                queue.append(WalkItem(d, item.depth + 1))

            files_processed += 1

        # Final outline
        if self._outline:
            yield "\n---\n"
            yield "## Global Flow Outline\n"
            yield self._outline + "\n"

    # Compatibility sink
    def run(self, *, stream: StreamFn = default_streamer) -> None:
        for chunk in self.stream_iter():
            stream(chunk)


# ------------------------------
# Specific workflows
# ------------------------------

@dataclass
class RepoWalkthroughWorkflow(_BaseWalkthrough):
    def stream_iter(self) -> Iterator[str]:
        initial = list(fetch_entry_point_files_as_list(self.repo_name))
        # IMPORTANT: yield from to forward the inner generator
        yield from self._walk(initial)


@dataclass
class DirWalkthroughWorkflow(_BaseWalkthrough):
    directory: str = ""

    def stream_iter(self) -> Iterator[str]:
        if list_files_in_dir is None:
            raise RuntimeError(
                "list_files_in_dir tool is not available. Please provide .tools.list_files_in_dir.list_files_in_dir."
            )
        try:
            files = list(list_files_in_dir(self.directory))  # type: ignore[misc]
        except Exception as e:
            yield f"**Failed to list files for directory `{self.directory}`: {e}**\n"
            return
        # Filter to code files
        initial = [p for p in files if looks_like_code_path(p)]
        if not initial:
            yield f"**No code files found in directory `{self.directory}`.**\n"
            return
        yield from self._walk(initial)


@dataclass
class FileWalkthroughWorkflow(_BaseWalkthrough):
    file_path: str = ""

    def stream_iter(self) -> Iterator[str]:
        if not looks_like_code_path(self.file_path):
            yield f"**Not a recognized code file:** {self.file_path}\n"
            return
        yield from self._walk([self.file_path])


# ------------------------------
# FastAPI wiring helpers (optional)
# ------------------------------

def walkthrough_generator_for_fastapi(*, llm: Any, repo_name: str, seed_prompt: str, user_question: str, config: WalkConfig | None = None) -> Iterator[str]:
    wf = RepoWalkthroughWorkflow(
        llm=llm,
        repo_name=repo_name,
        seed_prompt=seed_prompt,
        user_question=user_question,
        config=config or WalkConfig(),
    )
    return wf.stream_iter()


def dir_walkthrough_generator_for_fastapi(*, llm: Any, repo_name: str, directory: str, seed_prompt: str, user_question: str, config: WalkConfig | None = None) -> Iterator[str]:
    wf = DirWalkthroughWorkflow(
        llm=llm,
        repo_name=repo_name,
        seed_prompt=seed_prompt,
        user_question=user_question,
        directory=directory,
        config=config or WalkConfig(),
    )
    return wf.stream_iter()


def file_walkthrough_generator_for_fastapi(*, llm: Any, repo_name: str, file_path: str, seed_prompt: str, user_question: str, config: WalkConfig | None = None) -> Iterator[str]:
    wf = FileWalkthroughWorkflow(
        llm=llm,
        repo_name=repo_name,
        seed_prompt=seed_prompt,
        user_question=user_question,
        file_path=file_path,
        config=config or WalkConfig(),
    )
    return wf.stream_iter()


# ------------------------------
# Example usage + lightweight self-tests (pseudo; wire your real `llm`)
# ------------------------------
if __name__ == "__main__":
    # Patch tool funcs for isolated tests
    def _fake_entry_points(_: str) -> Sequence[str]:
        return ["src/main.py"]

    def _fake_code_file(path: str, repo: str) -> str:
        return """def run():\n    return 'ok'\n"""

    def _fake_xrefs(path: str, repo: str) -> Sequence[str]:
        # Pretend main depends on utils
        if path.endswith("main.py"):
            return ["src/utils.py", "src/utils.py"]  # dupe to test de-dupe
        return []

    def _fake_list_dir(path: str) -> Sequence[str]:
        if path == "src":
            return ["src/main.py", "src/utils.py", "src/README.md"]
        return []

    # Monkey-patch the imported names used by the workflow
    globals()["fetch_entry_point_files_as_list"] = _fake_entry_points
    globals()["fetch_code_file"] = _fake_code_file
    globals()["fetch_cross_file_refs_for_file_as_list"] = _fake_xrefs
    globals()["list_files_in_dir"] = _fake_list_dir

    class DummyLLM:
        def generate(self, *, prompt: str, stream: bool = False):
            text = "- Role: demo summary\n- Key APIs: run()\n- Flow: returns ok"
            if stream:
                # yield in two parts to exercise streaming
                parts = ["- Role: demo summary\n", "- Key APIs: run()\n- Flow: returns ok"]
                # Simulate Groq/OpenAI chunk envelopes
                class _Delta:
                    def __init__(self, c):
                        self.content = c
                class _Choice:
                    def __init__(self, c):
                        self.delta = _Delta(c)
                class _Chunk:
                    def __init__(self, c):
                        self.choices = [_Choice(c)]
                for t in parts:
                    yield _Chunk(t)
            else:
                return text

    # 1) Repo-level: streaming + outline
    wf_repo = RepoWalkthroughWorkflow(
        llm=DummyLLM(),
        repo_name="demo",
        seed_prompt="This repo is a demo program.",
        user_question="Explain how this code repo works end-to-end.",
        config=WalkConfig(max_depth=2, max_files=5),
    )
    out_repo = "".join(list(wf_repo.stream_iter()))
    assert "# Walkthrough for `demo`" in out_repo
    assert "## src/main.py" in out_repo
    assert "Global Flow Outline" in out_repo

    # 2) Dir-level: includes utils from directory listing
    wf_dir = DirWalkthroughWorkflow(
        llm=DummyLLM(),
        repo_name="demo",
        seed_prompt="seed",
        user_question="Explain this directory",
        directory="src",
        config=WalkConfig(max_depth=1, max_files=5),
    )
    out_dir = "".join(list(wf_dir.stream_iter()))
    assert "## src/main.py" in out_dir
    assert "## src/utils.py" in out_dir

    # 3) File-level: single file only
    wf_file = FileWalkthroughWorkflow(
        llm=DummyLLM(),
        repo_name="demo",
        seed_prompt="seed",
        user_question="Explain this file",
        file_path="src/utils.py",
        config=WalkConfig(max_depth=1, max_files=5),
    )
    out_file = "".join(list(wf_file.stream_iter()))
    assert "## src/utils.py" in out_file
    assert "## src/main.py" not in out_file

    # 4) Repo-level with no entry points -> should show graceful message
    globals()["fetch_entry_point_files_as_list"] = lambda _repo: []
    wf_empty_repo = RepoWalkthroughWorkflow(
        llm=DummyLLM(),
        repo_name="empty",
        seed_prompt="seed",
        user_question="Explain",
    )
    out_empty = "".join(list(wf_empty_repo.stream_iter()))
    assert "No code files found to explain" in out_empty

    # 5) Dir-level with no code files
    globals()["list_files_in_dir"] = lambda d: ["src/README.md"]
    wf_empty_dir = DirWalkthroughWorkflow(
        llm=DummyLLM(),
        repo_name="demo",
        seed_prompt="seed",
        user_question="Explain dir",
        directory="src",
    )
    out_empty_dir = "".join(list(wf_empty_dir.stream_iter()))
    assert "No code files found in directory `src`" in out_empty_dir

    # 6) File-level with non-code file
    wf_bad_file = FileWalkthroughWorkflow(
        llm=DummyLLM(),
        repo_name="demo",
        seed_prompt="seed",
        user_question="Explain",
        file_path="README.md",
    )
    out_bad_file = "".join(list(wf_bad_file.stream_iter()))
    assert "Not a recognized code file" in out_bad_file

    print("[Self-tests passed]")
