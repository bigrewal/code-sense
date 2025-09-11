"""
LLM-First Router using Groq (OpenAI-compatible tool calling)

Inputs:
- user_question: str
- repo_name: str
- seed_prompt: str (entry points + brief summaries + cross-file notes)

Behavior:
- Builds a compact seed digest (top-K paths) for grounding.
- Exposes **decision-support tools** the LLM can call (path search/normalize, list entry points, dir listing, xrefs, symbol lookups).
- Runs an OpenAI-style tool-calling loop with Groq `openai/gpt-oss-20b` until the model returns a final **RoutePlan JSON** in the assistant message.
- The router **does not** call workflow-selection tools; instead, it outputs a structured JSON (route + params) that your executor runs.
- Clarification flow is omitted for now per spec.

Extensibility:
- Add more decision tools by appending to `DECISION_TOOLS` and `_TOOL_DISPATCH`.
- Add new workflows by just allowing the model to output new `route` enums (executor can adopt later).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import traceback
from typing import Any, Dict, List, Optional, Sequence, Tuple, Callable
import json
import re

# ---- Project tools (some optional) ----
from .tools.fetch_entry_point_files import fetch_entry_point_files_as_list
from .tools.fetch_cross_file_refs_for_file import fetch_cross_file_refs_for_file_as_list
from .tools.fetch_code_file import fetch_code_file  # not used directly here

try:  # optional symbol tools
    from .tools.lookup_refs_for_def import lookup_refs_for_def  # type: ignore
except Exception:
    lookup_refs_for_def = None  # type: ignore

try:
    from .tools.lookup_def_usages import lookup_def_usages  # type: ignore
except Exception:
    lookup_def_usages = None  # type: ignore

try:
    from .tools.list_files_in_dir import list_files_in_dir  # type: ignore
except Exception:
    list_files_in_dir = None  # type: ignore

from .tools.fetch_code_file import fetch_code_file as fetch_code_file_tool # type: ignore

MODEL_NAME = "openai/gpt-oss-20b"
TOP_K_SEED = 20

# ------------------------------
# Types
# ------------------------------

ROUTE_VALUES = {
    "REPO_WALKTHROUGH": "User asks for an overall architecture or 'end-to-end' / 'overview' of the repo.",
    "DIR_WALKTHROUGH": "Question targets a directory/module or feature area that maps to a directory; include sub-directories (recursive=true).",
    "FILE_WALKTHROUGH": "Question names a specific file; resolve short names (e.g., 'main.py') to the canonical path using tools before returning.",
    "SYMBOL_EXPLAIN": "Question asks how a function/class/constant works (e.g., 'How does schedule_jobs() work?').",
    "SYMBOL_USAGES": "Question asks who/where a symbol is used (callers/call sites).",
    "FREEFORM_QA": "Topical or cross-cutting questions (e.g., 'How does auth work?') or when scope is unclear; return 3–7 seed-matched files as the initial focus."
}

ROUTE_HINTS = "\n".join(f"- {k}: {v}" for k, v in ROUTE_VALUES.items())

@dataclass
class RouteTargets:
    dirs: List[str]
    files: List[str]
    symbols: List[str]


@dataclass
class WorkflowLimits:
    max_files: int = 200
    max_depth: int = 3


@dataclass
class RoutePlan:
    route: str
    targets: RouteTargets
    workflow_limits: WorkflowLimits
    assumptions: List[str]
    confidence: float
    notes_for_workflow: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "route": self.route,
                "targets": asdict(self.targets),
                "workflow_limits": asdict(self.workflow_limits),
                "assumptions": self.assumptions,
                "confidence": self.confidence,
                "notes_for_workflow": self.notes_for_workflow,
            },
            ensure_ascii=False,
        )


# ------------------------------
# Seed digest
# ------------------------------

@dataclass
class SeedEntry:
    path: str
    summary: str
    tags: List[str]
    cross_ref_count: int


@dataclass
class SeedDigest:
    entries: List[SeedEntry]
    keywords_index: Dict[str, List[str]]  # keyword -> [paths]

    def known_paths(self) -> set:
        return {e.path for e in self.entries}


PATH_RE = re.compile(r"(?:^|\s)([\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|scala|rb|php|cs|cpp|c|h|hpp|m|mm|swift|sql|sh|bash|ps1|ya?ml|json))\b")


def _extract_paths(seed_prompt: str) -> List[str]:
    return list({m.group(1) for m in PATH_RE.finditer(seed_prompt or "")})


def _extract_entries(seed_prompt: str, top_k: int = TOP_K_SEED) -> List[SeedEntry]:
    lines = [ln.strip() for ln in (seed_prompt or "").splitlines() if ln.strip()]
    paths = _extract_paths(seed_prompt)
    seen = set()
    entries: List[SeedEntry] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        cand_idx = next((i for i, ln in enumerate(lines) if p in ln), -1)
        summary = ""
        if cand_idx != -1:
            summary = lines[cand_idx]
            if cand_idx + 1 < len(lines) and len(lines[cand_idx + 1]) < 160:
                summary = lines[cand_idx + 1]
        tags = [tok.lower() for tok in re.findall(r"[A-Za-z0-9_]+", summary) if len(tok) > 2][:8]
        xref = summary.lower().count("->") + summary.lower().count("import") + summary.lower().count("uses")
        entries.append(SeedEntry(path=p, summary=summary[:200], tags=tags[:8], cross_ref_count=xref))
    entries.sort(key=lambda e: (-(e.cross_ref_count), len(e.path)))
    return entries[:top_k]


def _build_keywords_index(entries: Sequence[SeedEntry]) -> Dict[str, List[str]]:
    idx: Dict[str, List[str]] = {}
    for e in entries:
        for t in e.tags:
            idx.setdefault(t, []).append(e.path)
    return idx


def build_seed_digest(seed_prompt: str, top_k: int = TOP_K_SEED) -> SeedDigest:
    entries = _extract_entries(seed_prompt, top_k=top_k)
    return SeedDigest(entries=entries, keywords_index=_build_keywords_index(entries))


def serialize_seed_digest_for_prompt(digest: SeedDigest) -> str:
    lines = ["path | tags | xref | summary"]
    for e in digest.entries:
        tags = ",".join(e.tags[:6])
        lines.append(f"{e.path} | {tags} | {e.cross_ref_count} | {e.summary}")
    return "\n".join(lines)


# ------------------------------
# Decision-support tool schemas (OpenAI-compatible)
# ------------------------------

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
]


# ------------------------------
# Router prompts
# ------------------------------

SYSTEM_PROMPT = (
    "You are a precise router for a code comprehension system.\n"
    "Use the available tools to ground your decision (e.g., normalize paths, list entry points/dirs, cross-refs, symbol lookups).\n"
    "\n"
    f"VALID ROUTES (choose exactly one): {list(ROUTE_VALUES.keys())}\n"
    "WHEN TO USE EACH ROUTE:\n"
    f"{ROUTE_HINTS}\n"
    "\n"
    "OUTPUT REQUIREMENTS:\n"
    "- Return a SINGLE JSON object with EXACT keys: route, targets{dirs,files,symbols}, workflow_limits{max_files,max_depth}, assumptions[], confidence, notes_for_workflow.\n"
    "- route MUST be one of the VALID ROUTES above (do NOT invent new routes like 'inspect').\n"
    "- targets.dirs/files/symbols MUST reference existing entities grounded in the seed digest or tool outputs. If the user provides a short file name (e.g., 'main.py'), use tools to resolve it to the canonical path BEFORE returning.\n"
    "- For DIR_WALKTHROUGH set recursive=true conceptually (include sub-dirs in your selection logic). For FREEFORM_QA, provide 3–7 seed-matched files with a brief rationale.\n"
    "- confidence MUST be a number between 0 and 1 (float), not a string.\n"
    "- If none of FILE/DIR/SYMBOL routes can be confidently chosen, prefer FREEFORM_QA.\n"
    "- Do NOT emit any prose outside the JSON.\n"
    "\n"
    f"TOOLS YOU MAY CALL (and ONLY these): {[tool['function']['name'] for tool in DECISION_TOOLS]}"
)


def _router_user_prompt(repo_name: str, user_question: str, digest: SeedDigest) -> str:
    table = serialize_seed_digest_for_prompt(digest)
    return (
        f"[REPO]\n{repo_name}\n\n"
        f"[QUESTION]\n{user_question}\n\n"
        f"[SEED DIGEST (top-{len(digest.entries)})]\n{table}\n\n"
        "Decide the best workflow route that will fully answer the question. "
        "Prefer the smallest sufficient scope. If the user references a file like 'main.py', use tools to find the canonical path first. "
        "Finally, output ONLY the JSON object described above."
    )


def _router_user_prompt(repo_name: str, user_question: str, seed_prompt: str) -> str:
    return (
        f"[REPO]\n{repo_name}\n\n"
        f"[QUESTION]\n{user_question}\n\n"
        f"[SEED DIGEST]\n{seed_prompt}\n\n"
        "Decide the best workflow route that will fully answer the question. "
        "Prefer the smallest sufficient scope. If the user references a file like 'main.py', use tools to find the canonical path first. "
        "Finally, output ONLY the JSON object described above."
    )

# ------------------------------
# Tool handlers (Python side)
# ------------------------------

class _ToolRuntime:
    def __init__(self, repo_name: str, digest: SeedDigest = None):
        self.repo_name = repo_name
        # self.digest = digest
        # self._index_paths = [e.path for e in digest.entries]

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


_TOOL_DISPATCH: Dict[str, Callable[["_ToolRuntime", Dict[str, Any]], Dict[str, Any]]] = {
    "list_entry_points": _ToolRuntime.list_entry_points,
    "list_dir": _ToolRuntime.list_dir,
    "get_cross_refs": _ToolRuntime.get_cross_refs,
    "lookup_symbol_usages": _ToolRuntime.lookup_symbol_usages,
    "lookup_symbol_refs": _ToolRuntime.lookup_symbol_refs,
    "fetch_code_file": _ToolRuntime.fetch_code_file,
}


# ------------------------------
# Router (tool-calling loop)
# ------------------------------

class Router:
    def __init__(self, groq_client: Any, *, temperature: float = 0.0):
        self.client = groq_client
        self.temperature = temperature

    def route(self, *, user_question: str, repo_name: str, seed_prompt: str) -> RoutePlan:
        print(f"Routing question: {user_question} in repo {repo_name}")
        # digest = build_seed_digest(seed_prompt)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _router_user_prompt(repo_name, user_question, seed_prompt)},
        ]

        run = _ToolRuntime(repo_name)

        # Tool-calling loop
        for _ in range(5):  # safety cap
            try:
                resp = self.client.generate(
                    messages=messages,
                    reasoning_effort="medium",
                    tools=DECISION_TOOLS,
                    tool_choice="auto",
                    temperature=self.temperature,
                    return_raw=True,
                    stream=False,
                )
                choice = resp.choices[0]
                msg = choice.message
                # tool_calls = getattr(msg, "tool_calls", None) or msg.get("tool_calls")
                tool_calls = msg.tool_calls
                if tool_calls:
                    for tc in tool_calls:
                        name = tc.function.name
                        raw = tc.function.arguments
                        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                        print(f"Invoking tool: {name} with args {args}")
                        handler = _TOOL_DISPATCH.get(name)
                        if handler is None:
                            result = {"error": f"unknown tool: {name}"}
                        else:
                            result = handler(run, args)
                        # Respond with tool result
                        # print(f"Tool result for {name}: {result}")
                        print(f"Reasoning behind tool use: {msg.reasoning}")
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": getattr(tc, "id", name),
                            "name": name,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                        # messages.append(
                        #     {
                        #         "role": "assistant", 
                        #         "tool_calls": [tc], 
                        #         "content": None
                        #     }
                        # )
                        messages.append(
                            {
                                "role": "assistant", 
                                "content": f"Called Tool: {name} with args {args} and here are the results: {result}. \n\n Don't use the same tool again"
                            }
                        )
                        # messages.append(tool_msg)
                    continue  # ask model again with new tool results
            
            except Exception as ex:
                traceback.print_exc()
                raise RuntimeError(f"Routing error: {str(ex)}")

            # No tool calls → expect final JSON content with RoutePlan
            content = msg.content.strip()
            data = {}
            try:
                data = json.loads(content or "{}")
                print(f"====== Parsed routing JSON ===== :\n {data}")
            except Exception:
                # If the model didn't output JSON, fallback to FREEFORM_QA
                return RoutePlan(
                    route="FREEFORM_QA",
                    targets=RouteTargets(dirs=[], files=[], symbols=[]),
                    workflow_limits=WorkflowLimits(),
                    assumptions=["Model did not return JSON; defaulted to FREEFORM_QA"],
                    confidence=0.5,
                    notes_for_workflow="Fallback route.",
                )

            # Normalize route values (accept FREEDOM_QA typo as FREEFORM_QA)
            route = str(data.get("route", "FREEFORM_QA")).upper()
            if route == "FREEDOM_QA":
                route = "FREEFORM_QA"
            if route not in ROUTE_VALUES:
                route = "FREEFORM_QA"

            targets = data.get("targets", {}) or {}
            dirs = list(dict.fromkeys(targets.get("dirs", [])))
            files = list(dict.fromkeys(targets.get("files", [])))
            symbols = list(dict.fromkeys(targets.get("symbols", [])))

            limits = data.get("workflow_limits", {}) or {}
            wf_limits = WorkflowLimits(
                max_files=int(limits.get("max_files", 200)),
                max_depth=int(limits.get("max_depth", 3)),
            )

            plan = RoutePlan(
                route=route,
                targets=RouteTargets(dirs=dirs, files=files, symbols=symbols),
                workflow_limits=wf_limits,
                assumptions=list(data.get("assumptions", [])),
                confidence=float(data.get("confidence", 0.75)),
                notes_for_workflow=str(data.get("notes_for_workflow", "")),
            )
            return plan

        # Safety fallback after too many tool turns
        return RoutePlan(
            route="FREEFORM_QA",
            targets=RouteTargets(dirs=[], files=[], symbols=[]),
            workflow_limits=WorkflowLimits(),
            assumptions=["Exceeded tool-call loop iterations; defaulted to FREEFORM_QA"],
            confidence=0.5,
            notes_for_workflow="Timeout fallback.",
        )


# ------------------------------
# Example usage + lightweight self-tests
# ------------------------------
if __name__ == "__main__":
    SEED = """
    src/main.py
    Main entrypoint -> imports src/utils.py
    src/utils.py
    Helpers for I/O and parsing
    services/auth/handler.py
    Auth flows and token checks
    """

    # Dummy Groq client that exercises tool loop
    class _Func:
        def __init__(self, name, arguments, _id="tc_1"):
            self.name = name
            self.arguments = arguments
            self.id = _id

    class _Msg:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []
        def get(self, k, d=None):
            return getattr(self, k, d)

    class _Choice:
        def __init__(self, message):
            self.message = message

    class DummyGroq:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    msgs = kwargs["messages"]
                    # Check last tool result to decide next step
                    last_tool = next((m for m in reversed(msgs) if m.get("role") == "tool"), None)
                    user = next(m for m in msgs if m["role"] == "user")["content"]

                    if "end-to-end" in user:
                        # Direct final JSON (no tools)
                        content = json.dumps({
                            "route": "REPO_WALKTHROUGH",
                            "targets": {"dirs": [], "files": [], "symbols": []},
                            "workflow_limits": {"max_files": 200, "max_depth": 3},
                            "assumptions": [],
                            "confidence": 0.9,
                            "notes_for_workflow": "overview",
                        })
                        return type("Resp", (), {"choices": [_Choice(_Msg(content=content))]})()

                    if last_tool is None and "main.py" in user:
                        # Ask to search for main.py
                        tc = type("TC", (), {"function": _Func("search_seed_paths", json.dumps({"query": "main.py"}))})()
                        return type("Resp", (), {"choices": [_Choice(_Msg(tool_calls=[tc]))]})()

                    if last_tool and json.loads(last_tool["content"]).get("paths"):
                        # We got search results; produce final routing JSON
                        paths = json.loads(last_tool["content"])['paths']
                        content = json.dumps({
                            "route": "FILE_WALKTHROUGH",
                            "targets": {"dirs": [], "files": [paths[0]], "symbols": []},
                            "workflow_limits": {"max_files": 200, "max_depth": 3},
                            "assumptions": [],
                            "confidence": 0.85,
                            "notes_for_workflow": "normalized from short name",
                        })
                        return type("Resp", (), {"choices": [_Choice(_Msg(content=content))]})()

                    # Fallback freeform
                    content = json.dumps({
                        "route": "FREEFORM_QA",
                        "targets": {"dirs": [], "files": ["src/main.py", "src/utils.py"], "symbols": []},
                        "workflow_limits": {"max_files": 200, "max_depth": 3},
                        "assumptions": ["seed-based start"],
                        "confidence": 0.7,
                        "notes_for_workflow": "seed-matched",
                    })
                    return type("Resp", (), {"choices": [_Choice(_Msg(content=content))]})()

    router = Router(DummyGroq())

    plan1 = router.route(user_question="Explain this repo end-to-end", repo_name="demo", seed_prompt=SEED)
    assert plan1.route == "REPO_WALKTHROUGH"

    plan2 = router.route(user_question="How does main.py work?", repo_name="demo", seed_prompt=SEED)
    assert plan2.route == "FILE_WALKTHROUGH" and plan2.targets.files[0] == "src/main.py"

    plan3 = router.route(user_question="How does auth work?", repo_name="demo", seed_prompt=SEED)
    assert plan3.route in {"FREEFORM_QA", "DIR_WALKTHROUGH"}

    print("[Router self-tests passed]")
