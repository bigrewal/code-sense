from __future__ import annotations

"""
Dependency-aware, embedding-free retriever.

Drop-in API:
    retrieve_records(question: str, jsonl_records: list[dict], llm=None, repo_name: str="repo") -> list[dict]

Key points:
- Uses a lexical TF-IDF prefilter (no embeddings).
- Leverages your Neo4j helper `get_cross_file_deps(repo_name, file_path)` for deps.
- Iterates through batches of 10 records; an LLM *only* chooses from that batch.
- Strict JSON validation of LLM output to avoid hallucinations.
- Deterministic post-ordering: Upstream → Seeds → Downstream → Other (topo inside zones).

Integration:
- Provide an `llm` with `.generate(prompt: str) -> str` (wire to your Groq client).
- Ensure `get_cross_file_deps` is available in the module scope at runtime.

This file also includes a small `__main__` smoke test with a stub Neo4j function and a mock LLM.
"""

from dataclasses import dataclass
from typing import List, Dict, Set, Tuple, Optional
import math
import json
import re
from collections import defaultdict, deque

from .db import Neo4jClient, get_neo4j_client
from .llm import GroqLLM  

# If you have your own module, import it there (left commented intentionally):
# from your_module import get_cross_file_deps

neo4j_client: Neo4jClient = get_neo4j_client()

# --------------------------
# Data structures
# --------------------------

@dataclass
class Record:
    file_path: str
    summary: str
    upstream_dep: List[str]
    downstream_dep: List[str]
    _tokens: List[str] | None = None


# --------------------------
# Tokenization & TF-IDF (embedding-free)
# --------------------------

def _normalize_path(fp: str) -> str:
    return fp.strip()


def _tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[^A-Za-z0-9]+", text.lower()) if t]


def _tokenize_record(r: Record) -> List[str]:
    if r._tokens is not None:
        return r._tokens
    path_tokens = _tokenize(r.file_path.replace("/", " ").replace(".", " ").replace("_", " ").replace("-", " "))
    summary_tokens = _tokenize(r.summary)
    r._tokens = path_tokens + summary_tokens
    return r._tokens


class TfidfIndex:
    def __init__(self, records: List[Record]):
        self.records = records
        self.N = len(records)
        self.df: Dict[str, int] = defaultdict(int)
        for rec in records:
            toks = _tokenize_record(rec)
            for tok in set(toks):
                self.df[tok] += 1
        self.idf: Dict[str, float] = {t: math.log((self.N + 1) / (df + 0.5)) + 1.0 for t, df in self.df.items()}

    def score(self, query: str, rec: Record, anchors: Set[str]) -> float:
        toks = _tokenize_record(rec)
        q_toks = _tokenize(query)
        tf: Dict[str, int] = defaultdict(int)
        for t in toks:
            tf[t] += 1
        score = 0.0
        for qt in q_toks:
            if qt in self.idf:
                score += tf.get(qt, 0) * self.idf[qt]
        for a in anchors:
            if a in toks:
                score += 2.0 * (self.idf.get(a, 1.0))
        base = rec.file_path.split("/")[-1].lower()
        for a in anchors:
            if a in base:
                score += 3.0
        return score

    def top_m(self, query: str, anchors: Set[str], m: int) -> List[int]:
        scored = [(i, self.score(query, self.records[i], anchors)) for i in range(self.N)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [i for i, s in scored[:m] if s > 0.0] or [i for i, s in scored[:m]]


# --------------------------
# LLM interaction
# --------------------------

def build_batch_prompt(
    question: str,
    needs: List[str],
    keepset_context: List[Tuple[str, str]],
    candidates: List[Record],
    repo_name: str,
) -> str:
    def fmt_keep() -> str:
        if not keepset_context:
            return "(none)"
        return "\n".join([f"- {fp} — {why}" for fp, why in keepset_context])

    def fmt_needs() -> str:
        if not needs:
            return "(none)"
        return "\n".join([f"- {n}" for n in needs[:10]])

    def fmt_candidate(i: int, r: Record) -> str:
        try:
            dependency_info = _cross_file_interactions_in_file(r.file_path, repo_name)
            ups = list(dependency_info.get("upstream", {}).get("files", []))
            dns = list(dependency_info.get("downstream", {}).get("files", [])) 
        except Exception:
            ups, dns = [], []
        return (
            f"{i}) {r.file_path} — {r.summary}\n"
            f"   upstream: [{', '.join(ups[:5])}]\n"
            f"   downstream: [{', '.join(dns[:5])}]"
        )

    cand_block = "\n\n".join([fmt_candidate(i + 1, c) for i, c in enumerate(candidates)])

    system = (
        "You select relevant repository files for answering a question.\n"
        "You MUST ONLY select from the 10 candidate items shown.\n"
        "If unsure, do not select; instead add a short 'new_need'.\n"
        "Never invent file paths or content.\n"
        "Return STRICT JSON matching the provided schema.\n"
    )

    user = f"""
QUESTION:
{question}

CURRENT NEEDS (short):
{fmt_needs()}

ALREADY KEPT (file + 1-line reason; you may DROP some):
{fmt_keep()}

CANDIDATES (choose any subset; or none):
{cand_block}

OUTPUT SCHEMA (STRICT JSON):
{{
  "select":[
    {{"file_path":"<from candidates>",
     "look_for":"<1–2 concrete things to inspect>",
     "why":"<max 25 words>",
     "hop_hint":"upstream|downstream|same|unknown"}}
  ],
  "new_needs":["<optional bullets>"],
  "drop":["<zero or more file_paths from 'ALREADY KEPT'>"]
}}
"""

    return system, user


# --------------------------
# Utility helpers
# --------------------------

def parse_llm_json(raw: str) -> Optional[dict]:
    """Extract the first JSON object from `raw`. Tolerant to pre/post text."""
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        snippet = raw[start : end + 1]
        return json.loads(snippet)
    except Exception:
        return None


def extract_anchors(question: str) -> Set[str]:
    """Collect anchors from the question: quoted strings, backticked ids, file-like tokens, and >2 char tokens."""
    anchors: Set[str] = set()
    # Quoted strings (handle two groups safely)
    for a, b in re.findall(r'"([^"]+)"|\'([^\']+)\'', question):
        if a:
            anchors.add(a.lower())
        if b:
            anchors.add(b.lower())
    # Backticked identifiers
    for m in re.findall(r"`([^`]+)`", question):
        anchors.add(m.lower())
    # Path-like tokens with extensions
    for m in re.findall(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+", question):
        anchors.add(m.lower())
    # General tokens (length >= 3)
    for t in _tokenize(question):
        if len(t) >= 3:
            anchors.add(t)
    return anchors


# ---- Neo4j-backed helpers for ordering & nudging ----

def _neighbors(repo_name: str, file_path: str) -> Tuple[List[str], List[str]]:
    try:
        dependency_info = _cross_file_interactions_in_file(file_path, repo_name)
        return list(dependency_info.get("upstream", {}).get("files", [])), list(dependency_info.get("downstream", {}).get("files", []))
    except Exception:
        return [], []


def _order_within_zone(repo_name: str, zone_paths: List[str], keepset_paths: Set[str]) -> List[str]:
    """Topologically order a set of paths using Neo4j deps restricted to the zone.
    Fallback to input order if a cycle prevents full topo sort.
    """
    zone_paths = list(dict.fromkeys(zone_paths))

    # Build local graph
    indeg: Dict[str, int] = {p: 0 for p in zone_paths}
    adj: Dict[str, Set[str]] = {p: set() for p in zone_paths}
    zone = set(zone_paths)
    for p in zone_paths:
        ups, _ = _neighbors(repo_name, p)
        for u in ups:  # Edge u -> p for upstream u
            if u in zone:
                adj.setdefault(u, set()).add(p)
                indeg[p] += 1
    # Kahn's algorithm
    q = deque([p for p, d in indeg.items() if d == 0])
    out: List[str] = []
    while q:
        v = q.popleft()
        out.append(v)
        for nb in adj.get(v, set()):
            indeg[nb] -= 1
            if indeg[nb] == 0:
                q.append(nb)
    if len(out) == len(zone_paths):
        return out
    # Cycle fallback: keep existing order but move any zero-indeg first
    seen = set(out)
    return out + [p for p in zone_paths if p not in seen]


# --------------------------
# Main entry
# --------------------------

def retrieve_records(
    question: str,
    jsonl_records: List[dict],
    llm: GroqLLM=None,
    repo_name: str = "repo",
) -> List[dict]:
    """Return ordered list of {file_path, look_for} for answering `question`.

    Parameters
    ----------
    question : str
    jsonl_records : list[dict] each with keys file_path, summary, upstream_dep, downstream_dep
    llm : object with .generate(prompt: str) -> str (plug your Groq client)
    repo_name : str used by your Neo4j helper
    """
    # ---- Config ----
    PREFILTER_M = 300
    BATCH_SIZE = 10
    KEEPSET_CAP = 60
    STABLE_STOP_ROUNDS = 3
    NEIGHBOR_CAP = 5

    # ---- Normalize & index ----
    records: List[Record] = [
        Record(
            file_path=_normalize_path(rec.get("file_path", "").strip()),
            summary=(rec.get("summary") or "").strip(),
            upstream_dep=list(rec.get("upstream_dep") or []),
            downstream_dep=list(rec.get("downstream_dep") or []),
        )
        for rec in jsonl_records
    ]
    if not records:
        return []

    path_to_idx: Dict[str, int] = {r.file_path: i for i, r in enumerate(records)}
    tfidf = TfidfIndex(records)
    anchors = extract_anchors(question)

    # ---- Prefilter ----
    pre_idx = tfidf.top_m(question, anchors, PREFILTER_M)
    pre_set = set(pre_idx)

    # ---- Loop state ----
    keepset: Dict[str, Tuple[int, str]] = {}  # file_path -> (idx, why)
    needs: List[str] = []
    visited: Set[int] = set()
    queue: deque[int] = deque(pre_idx)

    stable_rounds = 0
    while queue:
        # ---- Next batch ----
        batch: List[int] = []
        while queue and len(batch) < BATCH_SIZE:
            i = queue.popleft()
            if i in visited:
                continue
            visited.add(i)
            batch.append(i)
        if not batch:
            break

        candidates = [records[i] for i in batch]
        candidate_paths = {r.file_path for r in candidates}
        keepset_context = [(fp, why) for fp, (idx, why) in list(keepset.items())]
        system_prompt, user_prompt = build_batch_prompt(question, needs, keepset_context, candidates, repo_name)

        # ---- LLM call ----
        if llm is None:
            raw_out = json.dumps({"select": [], "new_needs": [], "drop": []})
        else:
            raw_out = llm.generate(prompt=user_prompt, system_prompt=system_prompt)  # your Groq client here

        payload = parse_llm_json(raw_out) or {"select": [], "new_needs": [], "drop": []}

        # ---- Validate & apply ----
        select: List[Dict[str, str]] = []
        for it in payload.get("select", []):
            if not isinstance(it, dict):
                continue
            fp = it.get("file_path")
            if not isinstance(fp, str) or fp not in candidate_paths:
                continue
            lf = (it.get("look_for") or "").strip()[:200]
            why = (it.get("why") or lf or "relevant to the question").strip()[:200]
            select.append({"file_path": fp, "look_for": lf, "why": why})

        new_needs = [str(n).strip()[:140] for n in payload.get("new_needs", []) if isinstance(n, str) and n.strip()]
        drops = [fp for fp in payload.get("drop", []) if isinstance(fp, str) and fp in keepset]

        changed = False
        for fp in drops:
            del keepset[fp]
            changed = True

        newly_selected_paths: List[str] = []
        for item in select:
            fp = item["file_path"]
            why = item["why"]
            idx = path_to_idx.get(fp)
            if idx is not None and fp not in keepset:
                keepset[fp] = (idx, why)
                newly_selected_paths.append(fp)
                changed = True
            elif idx is not None:
                keepset[fp] = (idx, why)  # refresh reason

        if len(keepset) > KEEPSET_CAP:
            scored = [(fp, tfidf.score(question, records[idx], anchors)) for fp, (idx, _) in keepset.items()]
            scored.sort(key=lambda x: x[1])
            for fp, _ in scored[: len(keepset) - KEEPSET_CAP]:
                del keepset[fp]
                changed = True

        if new_needs:
            seen = set()
            merged = []
            for n in needs + new_needs:
                if n not in seen:
                    merged.append(n)
                    seen.add(n)
            needs = merged[:10]

        # ---- Directional nudge via Neo4j ----
        if newly_selected_paths:
            need_text = " ".join(needs).lower()
            prefer = "both"
            if any(k in need_text for k in ["usage", "caller", "references", "who uses"]):
                prefer = "downstream"
            elif any(k in need_text for k in ["define", "definition", "where is", "depends on", "dependency"]):
                prefer = "upstream"

            neighbor_indices: List[int] = []
            for fp in newly_selected_paths:
                ups, dns = _neighbors(repo_name, fp)
                neigh_paths: List[str] = []
                if prefer in ("both", "upstream"):
                    neigh_paths.extend(ups[:NEIGHBOR_CAP])
                if prefer in ("both", "downstream"):
                    neigh_paths.extend(dns[:NEIGHBOR_CAP])
                for np in neigh_paths:
                    idx = path_to_idx.get(np)
                    if idx is not None and idx in pre_set and idx not in visited:
                        neighbor_indices.append(idx)
            for i in reversed(neighbor_indices):
                queue.appendleft(i)

        # ---- Stability ----
        if changed:
            stable_rounds = 0
        else:
            stable_rounds += 1
            if stable_rounds >= STABLE_STOP_ROUNDS:
                break

    if not keepset:
        return []

    # ---------------- Post-ordering with Neo4j ----------------
    keep_paths: Set[str] = set(keepset.keys())

    def seed_score(fp: str) -> float:
        idx = path_to_idx[fp]
        return tfidf.score(question, records[idx], anchors)

    anchor_hits = {fp for fp in keep_paths if any(a in fp.lower() for a in anchors)}
    top_lex = sorted(keep_paths, key=seed_score, reverse=True)[:5]
    seeds: Set[str] = set(top_lex) | anchor_hits

    def ancestors(paths: Set[str]) -> Set[str]:
        seen: Set[str] = set()
        dq = deque(list(paths))
        while dq:
            p = dq.popleft()
            ups, _ = _neighbors(repo_name, p)
            for u in ups:
                if u in keep_paths and u not in seen:
                    seen.add(u)
                    dq.append(u)
        return seen

    def descendants(paths: Set[str]) -> Set[str]:
        seen: Set[str] = set()
        dq = deque(list(paths))
        while dq:
            p = dq.popleft()
            _, dns = _neighbors(repo_name, p)
            for d in dns:
                if d in keep_paths and d not in seen:
                    seen.add(d)
                    dq.append(d)
        return seen

    core_zone = set(seeds)
    up_zone = ancestors(core_zone) - core_zone
    down_zone = descendants(core_zone) - core_zone
    other_zone = keep_paths - (up_zone | core_zone | down_zone)

    upstream_order = _order_within_zone(repo_name, list(up_zone), keep_paths)
    core_order = _order_within_zone(repo_name, list(core_zone), keep_paths)
    downstream_order = _order_within_zone(repo_name, list(down_zone), keep_paths)
    other_order = sorted(list(other_zone), key=seed_score, reverse=True)

    concat_paths = upstream_order + core_order + downstream_order + other_order
    seen_paths: Set[str] = set()
    final_paths: List[str] = []
    for p in concat_paths:
        if p not in seen_paths:
            seen_paths.add(p)
            final_paths.append(p)

    result: List[dict] = []
    for fp in final_paths:
        look_for = keepset.get(fp, (None, "key logic relevant to the question"))[1]
        result.append({"file_path": fp, "look_for": look_for})
    return result


def _cross_file_interactions_in_file(file_path: str, repo_id: str):
    """Infer cross-file interactions for a given file by finding references to and from definitions in other files."""

    # Downstream: file_path → other files
    downstream_query = """
    MATCH (ref:ASTNode {repo_id: $repo_id, file_path: $file_path, is_reference: true})
    -[:REFERENCES]->(ident:ASTNode)
    WHERE ident.file_path <> $file_path
    MATCH (def:ASTNode)
    WHERE def.node_id = ident.parent_id
    RETURN DISTINCT ref.name AS ref_name, def.node_type AS node_type, def.file_path AS def_file_path
    """

    # Upstream: other files → file_path
    upstream_query = """
    MATCH (ref:ASTNode {repo_id: $repo_id, is_reference: true})
    -[:REFERENCES]->(ident:ASTNode {file_path: $file_path})
    MATCH (def:ASTNode)
    WHERE def.node_id = ident.parent_id
    RETURN DISTINCT ref.file_path AS ref_file_path, ref.name AS ref_name, def.node_type AS node_type
    """

    with neo4j_client.driver.session() as session:
        # Downstream
        downstream_result = list(session.run(downstream_query, repo_id=repo_id, file_path=file_path))
        downstream_interactions = [
            f"{record['ref_name']} REFERENCES {record['node_type']} IN {record['def_file_path']}"
            for record in downstream_result
        ]
        downstream_files = {
            record['def_file_path'] for record in downstream_result if record['def_file_path'] != file_path
        }

        # Upstream
        upstream_result = list(session.run(upstream_query, repo_id=repo_id, file_path=file_path))
        upstream_interactions = [
            f"{record['ref_name']} IN {record['ref_file_path']} REFERENCES {record['node_type']} IN {file_path}"
            for record in upstream_result
        ]
        upstream_files = {
            record['ref_file_path'] for record in upstream_result if record['ref_file_path'] != file_path
        }

        return {
            "downstream": {
                "interactions": downstream_interactions,
                "files": downstream_files,
            },
            "upstream": {
                "interactions": upstream_interactions,
                "files": upstream_files,
            },
        }

# --------------------------
# Tests / Smoke checks
# --------------------------

class _SelectSpecificLLM:
    """Mock LLM that selects specific files if they appear in the candidate list."""

    def __init__(self, prefer: List[str]):
        self.prefer = prefer

    def generate(self, prompt: str) -> str:
        found: List[str] = []
        for fp in self.prefer:
            if fp in prompt:
                found.append(fp)
        select = [{"file_path": fp, "look_for": "key logic", "why": "matches question"} for fp in found]
        return json.dumps({"select": select, "new_needs": [], "drop": []})


if __name__ == "__main__":
    # Define a stub for Neo4j helper so this file can run standalone
    def get_cross_file_deps(repo_name: str, file_path: str) -> Tuple[List[str], List[str]]:
        deps = {
            "auth/routes.py": (["auth/oauth.py", "config/oauth.yml"], ["user/profile_service.py"]),
            "auth/oauth.py": (["config/oauth.yml"], ["auth/routes.py"]),
            "config/oauth.yml": ([], ["auth/oauth.py", "auth/routes.py"]),
            "models/user.py": ([], ["user/profile_service.py"]),
            "user/profile_service.py": (["models/user.py"], []),
        }
        return deps.get(file_path, ([], []))

    # Sample repo data
    sample = [
        {"file_path": "auth/routes.py", "summary": "Defines OAuth login and callback routes", "upstream_dep": ["auth/oauth.py", "config/oauth.yml"], "downstream_dep": ["user/profile_service.py"]},
        {"file_path": "auth/oauth.py", "summary": "OAuth helpers for token exchange and state", "upstream_dep": ["config/oauth.yml"], "downstream_dep": ["auth/routes.py"]},
        {"file_path": "config/oauth.yml", "summary": "OAuth provider settings and redirect URIs", "upstream_dep": [], "downstream_dep": ["auth/oauth.py", "auth/routes.py"]},
        {"file_path": "user/profile_service.py", "summary": "Updates user profile after successful login", "upstream_dep": ["models/user.py"], "downstream_dep": []},
        {"file_path": "models/user.py", "summary": "User model and persistence", "upstream_dep": [], "downstream_dep": ["user/profile_service.py"]},
    ]

    # Test 1: No LLM provided -> empty selection -> empty result
    print("TEST 1: No LLM (expect empty list)")
    out1 = retrieve_records("How does OAuth login update the user profile?", sample, llm=None, repo_name="demo")
    print(out1)

    # Test 2: Mock LLM that selects two auth files
    print("\nTEST 2: Mock LLM selects auth files (expect ordered context incl. config, oauth, routes, user, profile)")
    mock_llm = _SelectSpecificLLM(["auth/oauth.py", "auth/routes.py"])  # only selects from shown candidates
    out2 = retrieve_records("How does OAuth login update the user profile?", sample, llm=mock_llm, repo_name="demo")
    for item in out2:
        print(item)

    # Test 3: Stability stop (large unchanged streak) on tiny input
    print("\nTEST 3: Stability on tiny input (still returns list or empty, but no crash)")
    out3 = retrieve_records("Unrelated question", sample[:2], llm=_SelectSpecificLLM(["nonexistent.py"]))
    print(out3)
