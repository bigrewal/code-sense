from __future__ import annotations

import json
import hashlib
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# Assuming these imports exist from your codebase
from .db import get_mongo_client, get_neo4j_client, get_potential_entry_points
from .llm import GroqLLM

# === Constants =================================================================
MENTAL_MODEL_COL = "mental_model"

# Legacy constants retained for compatibility (still used by getters)
ROLLING_SUMMARY = "ROLLING_SUMMARY_V1"  # kept for migration/back-compat (not used by new build)
DOC_HUMAN = "REPO_ARCHITECTURE_V5"
DOC_RETR = "REPO_ARCHITECTURE_RETRIEVAL_V5"

# New collection for bottom-up component representation
COMPONENT_CARD = "COMPONENT_CARD_V1"


# === Helper Functions ==========================================================

def now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def stable_fingerprint(*parts: str) -> str:
    """Deterministic SHA256 over the provided parts (joined by \x1f)."""
    h = hashlib.sha256()
    h.update("\x1f".join(parts).encode("utf-8", errors="ignore"))
    return h.hexdigest()


# === Core Builder (Bottom-Up) ==================================================

class RepoArchBuilderRolling:
    """
    Bottom-up architecture builder.

    Pipeline:
    1) Build dependency graph (Neo4j) and assign depths (BFS from entry point).
    2) Process depths from max -> 0 and create terse "Component Cards" per file:
       - What it does (from BRIEF_FILE_OVERVIEW)
       - How it interacts with dependencies/parents
       - Where it lives (path)
    3) Assemble DOC_HUMAN and DOC_RETR *from cards* (no narrative enrichment).
    """

    def __init__(self, *, mongo=None, neo=None, llm: GroqLLM = None) -> None:
        self.mongo = mongo or get_mongo_client()
        self.neo = neo or get_neo4j_client()
        self.llm = llm or GroqLLM()
        self.mental = self.mongo[MENTAL_MODEL_COL]

    # ---- Public API -----------------------------------------------------------

    async def build(self, repo_id: str) -> Dict[str, Any]:
        """
        Build architecture overviews for all entry points (bottom-up).
        Returns dict with 'human' and 'retrieval' lists of documents.
        """
        entry_points = get_potential_entry_points(self.mongo, repo_id) or []
        if not entry_points:
            raise ValueError(f"No entry points found for repo {repo_id}")

        human_docs: List[Dict[str, Any]] = []
        retrieval_docs: List[Dict[str, Any]] = []

        for ep in entry_points:
            print(f"[bottom-up] Building architecture for entry point: {ep}")

            graph = self._build_graph(repo_id, ep)
            if not graph["depth_map"]:
                # No deps; still try to card-ify the entry point
                print(f"  No dependencies found for {ep}; generating single card.")
            cards_by_depth = await self._build_component_cards_bottom_up(repo_id, graph)

            # Render final documents from cards (no LLM enrichment beyond formatting)
            human_doc = self._render_human_doc(repo_id, ep, graph, cards_by_depth)
            self._persist_final_doc(repo_id, ep, human_doc, DOC_HUMAN)
            human_docs.append(human_doc)

            retr_doc = self._render_retrieval_doc(repo_id, ep, graph, cards_by_depth)
            self._persist_final_doc(repo_id, ep, retr_doc, DOC_RETR)
            retrieval_docs.append(retr_doc)

        return {"human": human_docs, "retrieval": retrieval_docs}

    # ---- Graph Construction ---------------------------------------------------

    def _build_graph(self, repo_id: str, entry_point: str) -> Dict[str, Any]:
        """
        Build directed graph (file -> downstream dependencies) via BFS from entry point.
        Returns:
          - depth_map: Dict[path, depth]
          - children: Dict[path, List[path]]
          - parents: Dict[path, List[path]]
          - files_by_depth: Dict[int, List[path]]
          - max_depth: int
          - entry_point: str
        """
        depth_map: Dict[str, int] = {entry_point: 0}
        children: Dict[str, List[str]] = defaultdict(list)
        parents: Dict[str, List[str]] = defaultdict(list)
        files_by_depth: Dict[int, List[str]] = defaultdict(list)

        queue = deque([entry_point])
        visited: Set[str] = {entry_point}

        while queue:
            file_path = queue.popleft()
            depth = depth_map[file_path]
            try:
                # Expecting (upstream, downstream) but only downstream is needed here
                _, downstream = self.neo.file_dependencies(file_path=file_path, repo_id=repo_id)
            except Exception as e:
                print(f"    Warning: Could not fetch dependencies for {file_path}: {e}")
                downstream = []

            for child in downstream or []:
                children[file_path].append(child)
                parents[child].append(file_path)
                if child not in visited:
                    visited.add(child)
                    depth_map[child] = depth + 1
                    queue.append(child)

        # Populate files_by_depth
        for path, d in depth_map.items():
            files_by_depth[d].append(path)

        max_depth = max(files_by_depth.keys()) if files_by_depth else 0

        return {
            "depth_map": depth_map,
            "children": children,
            "parents": parents,
            "files_by_depth": files_by_depth,
            "max_depth": max_depth,
            "entry_point": entry_point,
        }

    # ---- Component Cards (Bottom-Up) -----------------------------------------

    async def _build_component_cards_bottom_up(
        self,
        repo_id: str,
        graph: Dict[str, Any],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """
        Build/refresh Component Cards from deepest depth upward.
        Returns cards grouped by depth.
        """
        files_by_depth = graph["files_by_depth"]
        max_depth = graph["max_depth"]
        children = graph["children"]
        parents = graph["parents"]

        cards_by_depth: Dict[int, List[Dict[str, Any]]] = defaultdict(list)

        # Process depths from leaves to entry point
        for depth in range(max_depth, -1, -1):
            print(f"  [cards] Processing depth {depth}/{max_depth}")
            current_files = sorted(files_by_depth.get(depth, []))
            if not current_files:
                continue

            # Fetch briefs for this batch
            briefs = {b["file_path"]: b["brief"] for b in self._fetch_briefs_batch(repo_id, current_files)}

            for path in current_files:
                brief = briefs.get(path, "")
                # Neighbor sets (stable order)
                child_list = sorted(children.get(path, []))
                parent_list = sorted(parents.get(path, []))

                # Fingerprint for caching (brief + neighbors + depth)
                fp = stable_fingerprint(brief, "\n".join(child_list), "\n".join(parent_list), str(depth))

                cached = self._get_component_card(repo_id, path, fp)
                if cached:
                    cards_by_depth[depth].append(cached)
                    continue

                # Generate card (LLM-json), then persist
                card = await self._generate_component_card(
                    repo_id=repo_id,
                    path=path,
                    depth=depth,
                    brief_text=brief,
                    children=child_list,
                    parents=parent_list,
                )
                card["fingerprint"] = fp
                self._store_component_card(card)
                cards_by_depth[depth].append(card)

        return cards_by_depth

    def _fetch_briefs_batch(self, repo_id: str, file_paths: List[str]) -> List[Dict[str, str]]:
        """
        Fetch BRIEF_FILE_OVERVIEW for a batch of files.
        Returns list of {file_path, brief} dicts.
        """
        result = []
        for file_path in file_paths:
            doc = self.mental.find_one(
                {"repo_id": repo_id, "document_type": "BRIEF_FILE_OVERVIEW", "file_path": file_path},
                {"_id": 0, "data": 1},
            )
            brief = (doc or {}).get("data", "")
            if brief:
                result.append({"file_path": file_path, "brief": brief})
        return result

    def _get_component_card(self, repo_id: str, path: str, fingerprint: str) -> Optional[Dict[str, Any]]:
        """Return existing card if fingerprint matches (cache)."""
        doc = self.mental.find_one(
            {
                "repo_id": repo_id,
                "document_type": COMPONENT_CARD,
                "file_path": path,
                "fingerprint": fingerprint,
            },
            {"_id": 0},
        )
        return doc

    async def _generate_component_card(
        self,
        repo_id: str,
        path: str,
        depth: int,
        brief_text: str,
        children: List[str],
        parents: List[str],
    ) -> Dict[str, Any]:
        """
        Ask LLM for a *concise* card. Output is strict JSON for stability.
        """
        # Build deterministic prompt
        system_prompt = (
            "You create concise component cards for code files.\n"
            "Be terse, factual, and path-heavy. No fluff. Keep 1-3 sentences per field.\n"
            "If a field has nothing meaningful, use an empty string.\n"
            "Return ONLY a JSON object with these keys:\n"
            '["path","depth","what_it_does","key_responsibilities","dependencies_used","referenced_by",'
            '"inputs_outputs","notable_constraints"]\n'
            "Where:\n"
            '- path: backticked file path (string)\n'
            "- depth: integer depth\n"
            "- dependencies_used: array of backticked file paths\n"
            "- referenced_by: array of backticked file paths\n"
        )

        # Prepare neighbors as backticked paths
        deps_bt = [f"`{c}`" for c in children]
        refs_bt = [f"`{p}`" for p in parents]

        user_prompt = f"""
File path: `{path}`
Depth: {depth}

BRIEF_FILE_OVERVIEW:
{brief_text or "(none)"}

Dependencies used (children):
{json.dumps(deps_bt, ensure_ascii=False)}

Referenced by (parents):
{json.dumps(refs_bt, ensure_ascii=False)}

Task:
- Fill the JSON fields with concise content.
- Focus on what the component does, its interactions (deps & parents), and its location (path).
- Keep outputs terse (bullets or short sentences).
Return JSON only.
"""
        try:
            raw = await self.llm.generate_async(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.0,
                reasoning_effort="medium",
            )
            txt = raw.strip()
            # If model wrapped JSON in code fences, strip them
            if txt.startswith("```"):
                txt = txt.strip("`")
                # After stripping, try to locate first '{' and last '}'
                l = txt.find("{")
                r = txt.rfind("}")
                if l != -1 and r != -1:
                    txt = txt[l : r + 1]
            data = json.loads(txt)
        except Exception as e:
            print(f"    Error generating component card for {path}: {e}")
            # Fallback: minimal deterministic card from inputs
            data = {
                "path": f"`{path}`",
                "depth": depth,
                "what_it_does": (brief_text[:240] + "...") if brief_text else "",
                "key_responsibilities": "",
                "dependencies_used": [f"`{c}`" for c in children],
                "referenced_by": [f"`{p}`" for p in parents],
                "inputs_outputs": "",
                "notable_constraints": "",
            }

        # Normalize required fields
        card = {
            "repo_id": repo_id,
            "document_type": COMPONENT_CARD,
            "file_path": path,
            "path": data.get("path") or f"`{path}`",
            "depth": int(data.get("depth", depth)),
            "what_it_does": data.get("what_it_does", "") or "",
            "key_responsibilities": data.get("key_responsibilities", "") or "",
            "dependencies_used": data.get("dependencies_used", []) or [],
            "referenced_by": data.get("referenced_by", []) or [],
            "inputs_outputs": data.get("inputs_outputs", "") or "",
            "notable_constraints": data.get("notable_constraints", "") or "",
            "generated_at": now_iso(),
        }
        # Ensure arrays are arrays of strings
        card["dependencies_used"] = [str(x) for x in card["dependencies_used"]]
        card["referenced_by"] = [str(x) for x in card["referenced_by"]]
        return card

    def _store_component_card(self, card: Dict[str, Any]) -> None:
        """Upsert a component card keyed by repo + file_path + fingerprint."""
        key = {
            "repo_id": card["repo_id"],
            "document_type": COMPONENT_CARD,
            "file_path": card["file_path"],
            "fingerprint": card["fingerprint"],
        }
        self.mental.update_one(key, {"$set": card}, upsert=True)

    # ---- Final Documents (Rendered from Cards) --------------------------------

    def _render_human_doc(
        self,
        repo_id: str,
        entry_point: str,
        graph: Dict[str, Any],
        cards_by_depth: Dict[int, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """
        Render the human-friendly architecture doc (for onboarding).
        No LLM enrichment; deterministic formatting from cards.
        """
        depth_map = graph["depth_map"]
        max_depth = graph["max_depth"]

        # Overview: light, deterministic
        total_files = len(depth_map)
        overview = [
            f"This document explains how the repository is structured starting from the entry point `{entry_point}`.",
            f"It is organized bottom-up by dependency depth (0 = entry point). Total files discovered from this entry: **{total_files}**.",
            "Each component section explains what it does, how it interacts with other components, and its location (file path).",
        ]

        # Components grouped by depth
        components_sections: List[str] = []
        for depth in range(0, max_depth + 1):
            cards = cards_by_depth.get(depth, [])
            if not cards:
                continue
            components_sections.append(f"### Layer: Depth {depth}")
            for c in sorted(cards, key=lambda x: x["file_path"]):
                components_sections.append(
                    "\n".join(
                        [
                            f"- **{c['path']}**",
                            f"  - What it does: {c['what_it_does']}",
                            f"  - Responsibilities: {c['key_responsibilities']}",
                            f"  - Uses: {', '.join(c['dependencies_used']) if c['dependencies_used'] else '—'}",
                            f"  - Referenced by: {', '.join(c['referenced_by']) if c['referenced_by'] else '—'}",
                            f"  - I/O: {c['inputs_outputs'] or '—'}",
                            f"  - Constraints: {c['notable_constraints'] or '—'}",
                        ]
                    )
                )

        # Interactions (parents/children) — concise view
        interactions = ["Below are the primary call/data relationships extracted from the graph:"]
        parents = graph["parents"]
        for child, pls in sorted(parents.items()):
            if not pls:
                continue
            interactions.append(f"- `{child}` ⇐ referenced by {', '.join(f'`{p}`' for p in sorted(pls))}")

        # Key flows (simple derivation: shortest upward paths to entry)
        flows = self._derive_key_flows(graph, k=5)

        # Data & external systems: we don’t resolve types here; point to files touching env/clients by name heuristics
        data_external = self._derive_data_external(cards_by_depth)

        architecture_md = "\n\n".join(
            [
                f"# {entry_point}",
                "## Overview",
                "\n\n".join(overview),
                "## Architecture Components",
                "\n\n".join(components_sections) if components_sections else "_No components found._",
                "## Architecture Patterns",
                "- Layered by dependency depth (higher depth = lower-level dependency).\n"
                "- Orchestration resides near the entry point; leaf nodes encapsulate specific capabilities.",
                "## Key Flows",
                flows or "_No key flows derived._",
                "## Data & External Systems",
                data_external or "_No data or external systems detected._",
            ]
        )

        return {
            "repo_id": repo_id,
            "document_type": DOC_HUMAN,
            "entry_point": entry_point,
            "architecture": architecture_md,
            "generated_at": now_iso(),
        }

    def _render_retrieval_doc(
        self,
        repo_id: str,
        entry_point: str,
        graph: Dict[str, Any],
        cards_by_depth: Dict[int, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """
        Render the retrieval-optimized doc (dense index for an LLM agent).
        """
        depth_map = graph["depth_map"]
        files_by_depth = graph["files_by_depth"]
        children = graph["children"]
        parents = graph["parents"]
        max_depth = graph["max_depth"]

        # Component Index (one-liners)
        index_lines: List[str] = []
        for depth in range(0, max_depth + 1):
            for c in sorted(cards_by_depth.get(depth, []), key=lambda x: x["file_path"]):
                index_lines.append(
                    f"- {c['path']} | depth={c['depth']} | does: {c['what_it_does']} | uses: "
                    f"{', '.join(c['dependencies_used']) if c['dependencies_used'] else '—'} | "
                    f"refs: {', '.join(c['referenced_by']) if c['referenced_by'] else '—'}"
                )

        # Navigation Graph (adjacency lists)
        nav_sections: List[str] = ["### Downstream (uses)"]
        for p in sorted(children.keys()):
            ch = children.get(p, [])
            nav_sections.append(f"- `{p}` → {', '.join(f'`{x}`' for x in sorted(ch)) if ch else '—'}")
        nav_sections.append("\n### Upstream (referenced by)")
        for c in sorted(parents.keys()):
            up = parents.get(c, [])
            nav_sections.append(f"- `{c}` ← {', '.join(f'`{x}`' for x in sorted(up)) if up else '—'}")

        # Flow Index
        flows = self._derive_key_flows(graph, k=12)

        # Resource Map (heuristics)
        resource_map = self._derive_data_external(cards_by_depth)

        architecture_md = "\n\n".join(
            [
                f"# {entry_point} (Retrieval Index)",
                "## Component Index",
                "\n".join(index_lines) if index_lines else "_Empty._",
                "## Navigation Graph",
                "\n".join(nav_sections),
                "## Flow Index",
                flows or "_No flows._",
                "## Resource Map",
                resource_map or "_No resources._",
            ]
        )

        return {
            "repo_id": repo_id,
            "document_type": DOC_RETR,
            "entry_point": entry_point,
            "architecture": architecture_md,
            "generated_at": now_iso(),
        }

    def _persist_final_doc(self, repo_id: str, entry_point: str, doc: Dict[str, Any], doc_type: str) -> None:
        key = {"repo_id": repo_id, "document_type": doc_type, "entry_point": entry_point}
        self.mental.update_one(key, {"$set": doc}, upsert=True)

    # ---- Heuristics for flows & resources ------------------------------------

    def _derive_key_flows(self, graph: Dict[str, Any], k: int = 5) -> str:
        """
        Derive simple flows: for each leaf, walk one parent chain towards the entry point.
        Not guaranteed shortest in cycles; good enough for navigation hints.
        """
        depth_map = graph["depth_map"]
        parents = graph["parents"]
        entry = graph["entry_point"]

        # Leaves: nodes with no children
        leaves = [p for p, _ in depth_map.items() if len(graph["children"].get(p, [])) == 0]
        flows: List[str] = []

        for leaf in sorted(leaves)[:k]:
            path = [leaf]
            seen = {leaf}
            cur = leaf
            # Greedy walk towards shallower parents
            while cur != entry:
                ups = parents.get(cur, [])
                if not ups:
                    break
                # choose parent with smallest depth (closer to entry)
                nxt = min(ups, key=lambda x: depth_map.get(x, 10**9))
                if nxt in seen:
                    # cycle detected
                    path.append(f"... `{nxt}` (cycle)")
                    break
                path.append(nxt)
                seen.add(nxt)
                cur = nxt
            flows.append(" → ".join(f"`{p}`" for p in path[::-1]))  # entry ... leaf (reverse)

        if not flows:
            return ""
        header = "- Derived flows (entry → leaf):"
        return "\n".join([header] + [f"  - {line}" for line in flows])

    def _derive_data_external(self, cards_by_depth: Dict[int, List[Dict[str, Any]]]) -> str:
        """
        Very light heuristic: list files whose responsibilities mention DB, SQL, cache, HTTP, gRPC, queue, config, env.
        """
        keywords = ["db", "sql", "database", "cache", "redis", "http", "grpc", "queue", "kafka", "sqs",
                    "config", "environment", "env", "s3", "bucket", "filesystem", "file io", "oauth", "auth"]
        lines: List[str] = []
        for depth in sorted(cards_by_depth.keys()):
            for c in cards_by_depth[depth]:
                blob = " ".join([
                    c.get("what_it_does", ""),
                    c.get("key_responsibilities", ""),
                    c.get("inputs_outputs", ""),
                    c.get("notable_constraints", ""),
                ]).lower()
                if any(kw in blob for kw in keywords):
                    lines.append(f"- {c['path']}: {c['key_responsibilities'] or c['what_it_does']}")
        return "\n".join(lines)

# === Convenience Functions =====================================================

async def build_repo_architecture_v2(repo_id: str) -> Dict[str, Any]:
    """Build architecture overviews using bottom-up component cards."""
    builder = RepoArchBuilderRolling()
    return await builder.build(repo_id)


async def get_repo_architecture(repo_id: str, entry_point: str = None) -> Dict[str, Any]:
    """
    Get human-friendly architecture overview.
    If entry_point is None, returns all entry points.
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]

    if entry_point:
        doc = mental.find_one(
            {"repo_id": repo_id, "document_type": DOC_HUMAN, "entry_point": entry_point},
            {"_id": 0},
        )
        if not doc:
            raise ValueError(f"No architecture found for {repo_id}:{entry_point}")
        return doc
    else:
        docs = list(mental.find({"repo_id": repo_id, "document_type": DOC_HUMAN}, {"_id": 0}))
        if not docs:
            raise ValueError(f"No architecture found for repo {repo_id}")
        return {"entry_points": docs}


async def get_repo_architecture_retrieval(repo_id: str, entry_point: str = None) -> Dict[str, Any]:
    """
    Get retrieval-optimized architecture overview.
    If entry_point is None, returns all entry points.
    """
    mongo = get_mongo_client()
    mental = mongo[MENTAL_MODEL_COL]

    if entry_point:
        doc = mental.find_one(
            {"repo_id": repo_id, "document_type": DOC_RETR, "entry_point": entry_point},
            {"_id": 0},
        )
        if not doc:
            raise ValueError(f"No retrieval doc found for {repo_id}:{entry_point}")
        return doc
    else:
        docs = list(mental.find({"repo_id": repo_id, "document_type": DOC_RETR}, {"_id": 0}))
        if not docs:
            raise ValueError(f"No retrieval docs found for repo {repo_id}")
        return {"entry_points": docs}
