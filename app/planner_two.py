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

import re
from typing import Dict, Any, Iterator, Sequence, Callable
import json

from dataclasses import dataclass, field
from collections import deque
from typing import Callable, Sequence, Set, List, Any, Iterator, Optional
import textwrap

# ---- Provided tools (imported directly; no separate CodeTools protocol) ----
from .tools.fetch_entry_point_files import fetch_entry_point_files_as_list
from .tools.fetch_cross_file_refs_for_file import fetch_cross_file_refs_for_file_as_list
from .tools.fetch_code_file import fetch_code_file

from .router import RoutePlan

from .db import get_mongo_client


from .tools.fetch_code_file import fetch_code_file as fetch_code_file_tool
from .tools.lookup_refs_for_def import lookup_refs_for_def
from .tools.lookup_def_usages import lookup_def_usages
from .db import attention_db_runtime
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


# ----- Tool schemas & runtime -----
def _fn(name: str, description: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


DECISION_TOOLS: List[Dict[str, Any]] = [
    _fn(
        "list_entry_points",
        "Return likely repo entry point file paths.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    _fn(
        "list_dir",
        "List files inside a directory. If recursive=true, include sub-directories (best-effort).",
        {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "recursive": {"type": "boolean", "default": True},
            },
            "required": ["directory"],
            "additionalProperties": False,
        },
    ),
    _fn(
        "get_cross_refs",
        "Return cross-file references for a given path (dependencies).",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    _fn(
        "lookup_symbol_usages",
        "Return usage locations for a symbol.",
        {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    _fn(
        "lookup_symbol_refs",
        "Return definitions / referenced symbols related to a symbol.",
        {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    _fn(
        "fetch_code_file",
        "Fetch the full content of a code file by its path.",
        {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
            "additionalProperties": False,
        },
    ),
    _fn(
        "search_repo_vectordb",
        "Search the code file summaries in the Vector DB.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": True,
        },
    ),
]

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
    directory: List[str] = field(default_factory=list)

    def stream_iter(self) -> Iterator[str]:
        if list_files_in_dir is None:
            raise RuntimeError(
                "list_files_in_dir tool is not available. Please provide .tools.list_files_in_dir.list_files_in_dir."
            )

        for dir_path in self.directory:
            try:
                files = list(list_files_in_dir(dir_path))  # type: ignore[misc]
            except Exception as e:
                yield f"**Failed to list files for directory `{dir_path}`: {e}**\n"
                return

            initial = [p for p in files if looks_like_code_path(p)]
            if not initial:
                yield f"**No code files found in directory `{dir_path}`.**\n"
                return
            yield from self._walk(initial)


@dataclass
class FileWalkthroughWorkflow(_BaseWalkthrough):
    file_paths: List[str] = field(default_factory=list)

    def stream_iter(self) -> Iterator[str]:
        for file_path in self.file_paths:
            if not looks_like_code_path(file_path):
                yield f"**Not a recognized code file:** {file_path}\n"
                continue
        yield from self._walk(self.file_paths)


## Freeform QA planner
class FreeformPlanner:
    """LLM + tool-calling planner that outputs an ordered file-inspection plan.
    All planner-specific helpers, tool schemas, dispatch, and prompts are encapsulated here.
    """
    MODEL_NAME = "openai/gpt-oss-20b"
    PATH_RE = re.compile(r"(?:^|\s)([\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|scala|rb|php|cs|cpp|c|h|hpp|m|mm|swift|sql|sh|bash|ps1|ya?ml|json))\b")

    # ----- Data models (scoped to the planner) -----
    @dataclass
    class PlanStep:
        file: str
        look_for: str
        priority: int

    @dataclass
    class FreeformPlan:
        steps: List["FreeformPlanner.PlanStep"]
        notes: str


    class _ToolRuntime:
        def list_entry_points(self, _args: Dict[str, Any]) -> Dict[str, Any]:
            try:
                paths = fetch_entry_point_files_as_list(self.repo_name)
            except Exception:
                paths = []
            return {"paths": paths}

        def search_seed_paths(self, args: Dict[str, Any]) -> Dict[str, Any]:
            # q = (args.get("query") or "").lower()
            # lim = int(args.get("limit") or 10)
            # matches = [p for p in self._index_paths if q in p.lower()]
            return {"paths": fetch_entry_point_files_as_list(self.repo_name)}

        def list_dir(self, args: Dict[str, Any]) -> Dict[str, Any]:
            directory = args.get("directory") or ""
            recursive = bool(args.get("recursive", True))
            if list_files_in_dir is None:
                return {"paths": [], "warning": "list_files_in_dir unavailable"}
            try:
                paths = list(list_files_in_dir(directory))  # best-effort; recursion depends on host impl
            except Exception:
                paths = []
            return {"paths": paths, "recursive": recursive}

        def get_cross_refs(self, args: Dict[str, Any]) -> Dict[str, Any]:
            path = args.get("path") or ""
            try:
                _, refs = fetch_cross_file_refs_for_file_as_list(path, self.repo_name)
            except Exception:
                refs = []
            return {"paths": refs}

        def lookup_symbol_usages(self, args: Dict[str, Any]) -> Dict[str, Any]:
            sym = args.get("symbol") or ""
            if lookup_def_usages is None:
                return {"locations": [], "warning": "lookup_def_usages unavailable"}
            try:
                locs = list(lookup_def_usages(sym, self.repo_name))
            except Exception:
                locs = []
            return {"locations": locs}

        def lookup_symbol_refs(self, args: Dict[str, Any]) -> Dict[str, Any]:
            sym = args.get("symbol") or ""
            if lookup_refs_for_def is None:
                return {"refs": [], "warning": "lookup_refs_for_def unavailable"}
            try:
                refs = list(lookup_refs_for_def(sym, self.repo_name))
            except Exception:
                refs = []
            return {"refs": refs}
        
        def fetch_code_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
            path = args.get("file_path") or ""
            try:
                code = fetch_code_file_tool(path)
                return {"path": path, "code": code}
            except Exception as e:
                return {"path": path, "error": str(e), "code": ""}

        def search_repo_vectordb(self, args: Dict[str, Any]) -> Dict[str, Any]:
            query = args.get("query") or ""
            try:
                _, pack_items = attention_db_runtime._pack_blocking(self.repo_name, query)
            except Exception:
                pack_items = []

            response = []
            for record in pack_items:
                response.append({
                    "file_path": record.get("file_path"),
                    "summary": record.get("summary"),
                })

            return {"results": response}


    _TOOL_DISPATCH: Dict[str, Callable[["_ToolRuntime", Dict[str, Any]], Dict[str, Any]]] = {
        "list_entry_points": _ToolRuntime.list_entry_points,
        "list_dir": _ToolRuntime.list_dir,
        "get_cross_refs": _ToolRuntime.get_cross_refs,
        "lookup_symbol_usages": _ToolRuntime.lookup_symbol_usages,
        "lookup_symbol_refs": _ToolRuntime.lookup_symbol_refs,
        "fetch_code_file": _ToolRuntime.fetch_code_file,
        "search_repo_vectordb": _ToolRuntime.search_repo_vectordb,
    }

    # ----- Prompts & helpers -----
    PLANNER_SYSTEM = (
        "You are a planning agent for Freeform QA in a codebase. "
        "Use tools to identify the MOST RELEVANT files to inspect, in order, to fully answer the user's question. "
        "Prefer entry points and files referenced by them; include config/middleware as needed. "
        "Finally, return ONLY a JSON object with fields: steps[], notes. "
        "Each step: {file, look_for, priority (1..N)}. "
        "Use tools to normalize short names (e.g., 'main.py' → 'src/main.py'). "
        f"TOOLS YOU MAY CALL (and ONLY these): {[tool['function']['name'] for tool in DECISION_TOOLS]}"
    )

    @staticmethod
    def _extract_paths(seed_prompt: str) -> List[str]:
        return list({m.group(1) for m in FreeformPlanner.PATH_RE.finditer(seed_prompt or "")})

    def _build_seed_table(self, seed_prompt: str, top_k: int = 30) -> str:
        lines = [ln.strip() for ln in (seed_prompt or "").splitlines() if ln.strip()]
        paths = self._extract_paths(seed_prompt)[:top_k]
        rows = ["path | hint"]
        for p in paths:
            idx = next((i for i, ln in enumerate(lines) if p in ln), -1)
            hint = lines[idx + 1] if idx != -1 and idx + 1 < len(lines) and len(lines[idx + 1]) < 160 else ""
            rows.append(f"{p} | {hint}")
        return "\n".join(rows)

    def _planner_user(self, repo: str, question: str, seed_prompt: str) -> str:
        return textwrap.dedent(
            f"""
            [REPO] {repo}
            [QUESTION] {question}
            [SEED PROMPT]
            {seed_prompt}

            Use the seed prompt to figure out the files needed to answer the question.
            Avoid duplicates. Keep 'look_for' short and concrete.
            Output ONLY JSON: {{"steps": [...], "notes": "..."}}
            """
        ).strip()

    # ----- Public API -----
    def __init__(self, llm: Any, *, model: str = MODEL_NAME, temperature: float = 0.1):
        self.llm = llm
        self.model = model
        self.temperature = temperature

    def plan(self, *, repo_name: str, seed_prompt: str, user_question: str) -> "FreeformPlanner.FreeformPlan":
        messages = [
            {"role": "system", "content": self.PLANNER_SYSTEM},
            {"role": "user", "content": self._planner_user(repo_name, user_question, seed_prompt)},
        ]
        run = FreeformPlanner._ToolRuntime(repo_name, seed_prompt)

        for _ in range(8):
            resp = self.llm.generate(
                messages=messages,
                tools=DECISION_TOOLS,
                reasoning_effort="medium",
                tool_choice="auto",
                temperature=self.temperature,
                stream=False,
                response_format=None,
                return_raw=True,
            )
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = msg.tool_calls
            if tool_calls:
                for tc in tool_calls:
                    name = tc.function.name
                    raw = tc.function.arguments
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    handler = FreeformPlanner._TOOL_DISPATCH.get(name)
                    if handler is None:
                        result = {"error": f"unknown tool: {name}"}
                    else:
                        result = handler(run, args)
                    messages.append({"role": "assistant", "tool_calls": [tc], "content": None})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": getattr(tc, "id", name),
                        "name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                continue

            # Expect final JSON plan
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            try:
                data = json.loads(content or "{}")
            except Exception:
                data = {"steps": [], "notes": "model failed to return JSON"}

            steps_raw = data.get("steps", []) or []
            steps: List[FreeformPlanner.PlanStep] = []
            seen = set()
            for i, s in enumerate(steps_raw, start=1):
                fp = str(s.get("file", "")).strip()
                lf = str(s.get("look_for", "")).strip() or "general overview"
                pr = int(s.get("priority", i))
                if not fp or fp in seen:
                    continue
                seen.add(fp)
                steps.append(FreeformPlanner.PlanStep(file=fp, look_for=lf[:200], priority=pr))
            steps.sort(key=lambda x: x.priority)
            notes = str(data.get("notes", "")).strip()
            return FreeformPlanner.FreeformPlan(steps=steps[:7], notes=notes[:400])

        # Fallback empty plan
        return FreeformPlanner.FreeformPlan(steps=[], notes="timeout")



# ------------------------------
# FastAPI wiring helpers (optional)
# ------------------------------

def repo_walkthrough_generator_for_fastapi(*, llm: Any, repo_name: str, seed_prompt: str, user_question: str, config: WalkConfig | None = None) -> Iterator[str]:
    wf = RepoWalkthroughWorkflow(
        llm=llm,
        repo_name=repo_name,
        seed_prompt=seed_prompt,
        user_question=user_question,
        config=config or WalkConfig(),
    )
    return wf.stream_iter()


def dir_walkthrough_generator_for_fastapi(*, llm: Any, repo_name: str, directory: List[str], seed_prompt: str, user_question: str, config: WalkConfig | None = None) -> Iterator[str]:
    wf = DirWalkthroughWorkflow(
        llm=llm,
        repo_name=repo_name,
        seed_prompt=seed_prompt,
        user_question=user_question,
        directory=directory,
        config=config or WalkConfig(),
    )
    return wf.stream_iter()


def file_walkthrough_generator_for_fastapi(*, llm: Any, repo_name: str, file_paths: List[str], seed_prompt: str, user_question: str, config: WalkConfig | None = None) -> Iterator[str]:
    wf = FileWalkthroughWorkflow(
        llm=llm,
        repo_name=repo_name,
        seed_prompt=seed_prompt,
        user_question=user_question,
        file_paths=file_paths,
        config=config or WalkConfig(),
    )
    return wf.stream_iter()


def freeform_qa_generator_for_fastapi(
    *,
    llm: Any,
    repo_name: str,
    seed_prompt: str,
    user_question: str,
    initial_files: Optional[Sequence[str]] = None,
    config: Optional[WalkConfig] = None,
) -> Iterator[str]:
    planner = FreeformPlanner(llm)
    plan = planner.plan(repo_name=repo_name, seed_prompt=seed_prompt, user_question=user_question)
    if not plan.steps:
        yield "**Planner produced no steps; falling back to entry points.**\n"
        initial = fetch_entry_point_files_as_list(repo_name)
        for f in initial[:5]:
            yield f"\n# Focus file: `{f}`\n"
            yield from file_walkthrough_generator_for_fastapi(
                llm=llm, repo_name=repo_name, file_path=f, seed_prompt=seed_prompt, user_question=user_question, config=config or WalkConfig()
            )
        return


    yield "# Freeform QA Plan\n"
    if plan.notes:
        yield f"> Notes: {plan.notes}\n"


    for step in plan.steps:
        yield f"\n## {step.priority}. {step.file}\n"
        if step.look_for:
            yield f"- Look for: {step.look_for}\n"
        yield from file_walkthrough_generator_for_fastapi(
            llm=llm,
            repo_name=repo_name,
            file_path=step.file,
            seed_prompt=seed_prompt,
            user_question=user_question,
            config=config or WalkConfig(),
        )


def execute_route(
    *,
    route_plan: RoutePlan,               # RoutePlan
    llm,                      # your GroqLLM
    repo_name: str,
    seed_prompt: str,
    user_question: str,
) -> Iterator[str]:
    cfg = WalkConfig(
        max_depth=route_plan.workflow_limits.max_depth,
        max_files=route_plan.workflow_limits.max_files,
    )

    r = route_plan.route
    T = route_plan.targets

    if r == "REPO_WALKTHROUGH":
        yield from repo_walkthrough_generator_for_fastapi(
            llm=llm, repo_name=repo_name, seed_prompt=seed_prompt, user_question=user_question, config=cfg
        )
        return
    elif r == "DIR_WALKTHROUGH":
        yield from dir_walkthrough_generator_for_fastapi(
            llm=llm, repo_name=repo_name, seed_prompt=seed_prompt, user_question=user_question, directory=T.dirs, config=cfg
        )
        return
    elif r == "FILE_WALKTHROUGH":
        yield from file_walkthrough_generator_for_fastapi(
            llm=llm, repo_name=repo_name, seed_prompt=seed_prompt, user_question=user_question, file_paths=T.files, config=cfg
        )
        return
    elif r == "FREEFORM_QA":
        yield from freeform_qa_generator_for_fastapi(
            llm=llm, repo_name=repo_name, seed_prompt=seed_prompt, user_question=user_question, config=cfg
        )
    else:
        print(f"**Unsupported route `{r}`.**\n")
        print(f"Target directories: {T.dirs}")
        yield f"**Unsupported route `{r}`.**\n"

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
