# stages/mental_model.py

import re
from typing import Any, Dict, List, Tuple
from pathlib import Path

from .core.base import PipelineStage, StageResult
from ..db import Neo4jClient, get_neo4j_client, get_mongo_client
from ..llm import GroqLLM

DEF_NODE_TYPES = {"function_definition", "class_definition"}

class RecordGenStage(PipelineStage):
    def __init__(self, config: dict | None = None):
        super().__init__("Record Generation", config or {})
        self.neo4j_client: Neo4jClient = get_neo4j_client()
        self.llm_client = GroqLLM()
        self.mongo_client = get_mongo_client()
        self.collection = self.mongo_client["mental_model"]
        self.job_id = self.config.get("job_id", "unknown")

    async def execute(self, input_data: dict) -> StageResult:
        try:
            s3_info = input_data.get("s3_info")
            repo_id = input_data.get("repo_name") or input_data["s3_info"].repo_info.repo_id
            print(f"Job {self.job_id}: Building record for repo: {repo_id}")

            # Fetch all def nodes with coordinates & file paths
            defs = self._fetch_def_nodes(repo_id)

            # Precompute name/kind/signature/snippet for ALL defs so we can reference callers by name/kind in summaries
            def_index: Dict[str, Dict[str, Any]] = {}
            for d in defs:
                nid = d["node_id"]
                node_type = d.get("node_type") or ""
                kind = "function" if node_type == "function_definition" else "class"
                file_path = d.get("file_path") or ""

                snippet = self._read_def_snippet(
                    file_path=file_path,
                    start_line=d.get("start_line"),
                    start_col=d.get("start_column"),
                    end_line=d.get("end_line"),
                    end_col=d.get("end_column"),
                )
                name, signature = self._extract_symbol_and_signature(kind, snippet)
                if not name:
                    name = self._fetch_symbol_from_ast(nid)
                if name and not signature:
                    signature = f"{name}(…)" if kind == "function" else f"{name}()"

                def_index[nid] = {
                    "name": name or "",
                    "kind": kind,
                    "signature": signature or "",
                    "snippet": snippet or "",
                }

            def_docs: List[Dict[str, Any]] = []
            for d in defs:
                node_id = d["node_id"]
                node_type = d.get("node_type") or ""
                file_path = d.get("file_path") or ""
                kind = "function" if node_type == "function_definition" else "class"

                meta = def_index.get(node_id, {})
                name = meta.get("name", "")
                signature = meta.get("signature", "")
                snippet = meta.get("snippet", "")

                summary = self._summarize(node_id, kind, name, signature, snippet)

                neighbors_up_ids = self._neighbors_up(node_id, file_path)
                neighbors_down_ids = self._neighbors_down(node_id, file_path)

                # ---- augment summary with upstream (callers) context: "This is used by: Name (kind), ..."
                if neighbors_up_ids:
                    used_by_labels: List[str] = []
                    for uid in neighbors_up_ids:
                        umeta = def_index.get(uid, {})
                        uname = umeta.get("name", "")
                        ukind = umeta.get("kind", "")
                        if uname:
                            used_by_labels.append(f"{uname} ({ukind})")
                    if used_by_labels:
                        # keep it concise: show up to 3, then ellipsis if more
                        preview = ", ".join(used_by_labels[:3])
                        ellipsis = "…" if len(used_by_labels) > 3 else ""
                        base = (summary or "").strip()
                        if base and not base.endswith("."):
                            base += "."
                        summary = f"{base} This is used by: {preview}{ellipsis}."

                doc = {
                    "id": node_id,
                    "type": "DEFINITIONS_RECORD",
                    "name": name or "",
                    "signature": signature or "",
                    "location": {
                        "start_line": d.get("start_line"),
                        "start_column": d.get("start_column"),
                        "end_line": d.get("end_line"),
                        "end_column": d.get("end_column"),
                    },
                    "file_path": file_path,
                    "repo_name": repo_id,
                    "dependencies": {
                        "upstream":   [{"id": x} for x in sorted(neighbors_up_ids)],
                        "downstream": [{"id": x} for x in sorted(neighbors_down_ids)],
                    },
                    "summary": summary or "",
                }
                def_docs.append(doc)

            # Upsert compact definition docs
            self._bulk_upsert(def_docs)
            print(f"Job {self.job_id}: DEFINITIONS_RECORD stored: {len(def_docs)} entries")

            return StageResult(
                success=True,
                data={"repo_name": repo_id, "count": len(def_docs), "s3_info": s3_info},
                metadata={"stage": self.name},
            )

        except Exception as e:
            import traceback
            traceback.print_exc()
            return StageResult(
                success=False,
                data=None,
                metadata={"stage": self.name},
                error=str(e),
            )


    # ---------- Neo4j ----------

    def _fetch_def_nodes(self, repo_id: str) -> List[Dict[str, Any]]:
        """
        Bring back enough coordinates to reconstruct the source snippet.
        """
        cypher = """
        MATCH (def:ASTNode)
        WHERE def.is_definition = true
          AND def.node_type IN $types
          AND def.node_id STARTS WITH $repo_id_prefix
        RETURN def.node_id AS node_id,
               def.node_type AS node_type,
               def.file_path AS file_path,
               def.start_line AS start_line,
               def.start_column AS start_column,
               def.end_line AS end_line,
               def.end_column AS end_column
        """
        with self.neo4j_client.driver.session() as session:
            res = session.run(
                cypher,
                types=list(DEF_NODE_TYPES),
                repo_id_prefix=repo_id + ":",
            )
            return [r.data() for r in res]

    def _fetch_symbol_from_ast(self, def_node_id: str) -> str:
        """
        For Python trees, the function/class name appears as an `identifier` leaf
        under the definition node. Grab the earliest identifier by traversal order.
        """
        cypher = """
        MATCH (def:ASTNode {node_id: $def_node_id})
        MATCH path = (def)-[rels:CONTAINS*..4]->(leaf:ASTNode)
        WHERE leaf.node_type = 'identifier'
        WITH leaf, reduce(s=0, r IN rels | s + coalesce(r.sequence, 1)) AS order_key
        RETURN leaf.name AS ident
        ORDER BY order_key ASC
        LIMIT 1
        """
        with self.neo4j_client.driver.session() as session:
            res = session.run(cypher, def_node_id=def_node_id)
            rec = res.single()
            return (rec and (rec["ident"] or "").strip()) or ""

    def _neighbors_up(self, def_node_id: str, def_file: str) -> List[str]:
        """
        Definitions (functions/classes) OUTSIDE this definition that reference/call
        something inside this definition (incoming REFERENCES).
        Returns distinct definition node_ids.
        """
        cypher = """
        MATCH (def:ASTNode {node_id: $def_node_id})
        MATCH (def)-[:CONTAINS*]->(def_leaf:ASTNode)
        MATCH (ref_leaf:ASTNode)-[:REFERENCES]->(def_leaf)
        MATCH (ref_def:ASTNode)-[:CONTAINS*]->(ref_leaf)
        WHERE ref_def.is_definition = true
        AND ref_def.node_type IN $types
        AND ref_def.node_id <> $def_node_id
        AND NOT (def)-[:CONTAINS*]->(ref_def)   // exclude nested-in-self
        RETURN DISTINCT ref_def.node_id AS dep_id
        """
        with self.neo4j_client.driver.session() as session:
            res = session.run(
                cypher,
                def_node_id=def_node_id,
                types=list(DEF_NODE_TYPES),
            )
            return sorted([r["dep_id"] for r in res])

    def _neighbors_down(self, def_node_id: str, def_file: str) -> List[str]:
        """
        Definitions (functions/classes) OUTSIDE this definition that are referenced/called
        by something inside this definition (outgoing REFERENCES).
        Returns distinct definition node_ids.
        """
        cypher = """
        MATCH (def:ASTNode {node_id: $def_node_id})
        MATCH (def)-[:CONTAINS*]->(my_leaf:ASTNode)
        MATCH (my_leaf)-[:REFERENCES]->(tgt_leaf:ASTNode)
        MATCH (tgt_def:ASTNode)-[:CONTAINS*]->(tgt_leaf)
        WHERE tgt_def.is_definition = true
        AND tgt_def.node_type IN $types
        AND tgt_def.node_id <> $def_node_id
        AND NOT (def)-[:CONTAINS*]->(tgt_def)   // exclude nested-in-self
        RETURN DISTINCT tgt_def.node_id AS dep_id
        """
        with self.neo4j_client.driver.session() as session:
            res = session.run(
                cypher,
                def_node_id=def_node_id,
                types=list(DEF_NODE_TYPES),
            )
            return sorted([r["dep_id"] for r in res])

    def _fetch_all_def_dependencies(self, repo_id: str) -> Dict[str, Dict[str, set]]:
        """
        For every definition in the repo, compute:
          - downstream: defs this def references (calls/uses)
          - upstream:   defs that reference this def (callers)
        Returns a map: def_id -> {"upstream": set(ids), "downstream": set(ids)}
        """
        cypher = """
        MATCH (d:ASTNode)
        WHERE d.is_definition = true
          AND d.node_type IN $types
          AND d.node_id STARTS WITH $repo_id_prefix

        // downstream: d -> callee_def
        CALL {
          WITH d
          OPTIONAL MATCH (d)-[:CONTAINS*]->(my_leaf:ASTNode)-[:REFERENCES]->(tgt_leaf:ASTNode)
          OPTIONAL MATCH (callee_def:ASTNode)-[:CONTAINS*]->(tgt_leaf)
          WHERE callee_def.is_definition = true
            AND callee_def.node_type IN $types
            AND callee_def.node_id <> d.node_id
          RETURN collect(DISTINCT callee_def.node_id) AS downstream
        }

        // upstream: caller_def -> d
        CALL {
          WITH d
          OPTIONAL MATCH (caller_def:ASTNode)-[:CONTAINS*]->(ref_leaf:ASTNode)-[:REFERENCES]->(my_leaf:ASTNode)
          OPTIONAL MATCH (d)-[:CONTAINS*]->(my_leaf)
          WHERE caller_def.is_definition = true
            AND caller_def.node_type IN $types
            AND caller_def.node_id <> d.node_id
          RETURN collect(DISTINCT caller_def.node_id) AS upstream
        }

        RETURN d.node_id AS def_id, upstream, downstream
        """
        out: Dict[str, Dict[str, set]] = {}
        with self.neo4j_client.driver.session() as session:
            res = session.run(
                cypher,
                types=list(DEF_NODE_TYPES),
                repo_id_prefix=repo_id + ":",
            )
            for r in res:
                def_id = r["def_id"]
                upstream = set((r.get("upstream") or []))
                downstream = set((r.get("downstream") or []))
                out[def_id] = {"upstream": upstream, "downstream": downstream}
        return out

    # ---------- Source reading & parsing ----------

    def _read_def_snippet(
        self,
        file_path: str,
        start_line: int | None,
        start_col: int | None,
        end_line: int | None,
        end_col: int | None,
    ) -> str:
        """
        Return the exact source slice for the definition node.
        Falls back gracefully if file/coords are missing.
        """
        if not file_path or start_line is None or start_col is None or end_line is None or end_col is None:
            return ""
        p = Path(file_path)
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines(True)
        except Exception:
            return ""
        try:
            if start_line == end_line:
                return lines[start_line][start_col:end_col]
            buf = []
            for i in range(start_line, end_line + 1):
                if i == start_line:
                    buf.append(lines[i][start_col:])
                elif i == end_line:
                    buf.append(lines[i][:end_col])
                else:
                    buf.append(lines[i])
            return "".join(buf)
        except Exception:
            return ""

    def _extract_symbol_and_signature(self, kind: str, snippet: str) -> Tuple[str, str]:
        """
        Parse the header line from the snippet to get symbol & signature.
        Works for Python `def` and `class`.
        """
        if not snippet:
            return "", ""

        first_line = snippet.splitlines()[0].strip()

        if kind == "function":
            # def name(args):
            m = re.match(r"def\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*:", first_line)
            if m:
                name, args = m.group(1), m.group(2)
                return name, f"{name}({args})"
            m2 = re.search(r"def\s+([A-Za-z_]\w*)", first_line)
            if m2:
                name = m2.group(1)
                return name, f"{name}(…)"
            return "", ""

        # class Name(bases):
        m = re.match(r"class\s+([A-Za-z_]\w*)\s*(\((.*?)\))?\s*:", first_line)
        if m:
            name = m.group(1)
            bases = m.group(3) or ""
            sig = f"{name}({bases})" if bases else f"{name}()"
            return name, sig

        m2 = re.search(r"class\s+([A-Za-z_]\w*)", first_line)
        if m2:
            name = m2.group(1)
            return name, f"{name}()"

        return "", ""

    # ---------- LLM summary with caching ----------

    def _summarize(self, doc_id: str, kind: str, symbol: str, signature: str, snippet: str) -> str:
        """
        If a DEFINITIONS_RECORD for this id already exists with a summary,
        reuse it instead of calling LLM.
        """
        existing = self.collection.find_one({"id": doc_id, "type": "DEFINITIONS_RECORD"}, {"summary": 1})
        if existing and existing.get("summary"):
            return existing["summary"]

        # If we still have no usable snippet, avoid a low-signal LLM call.
        if not snippet and not symbol:
            return ""

        system_prompt = (
            "You are a code analyst. Summarize code constructs in one plain sentence. "
            "Avoid restating parameter names unless meaningful. Keep it under 25 words."
        )
        prompt = (
            f"Summarize this {kind} in one sentence.\n\n"
            f"Name: {symbol or '(unknown)'}\n"
            f"Signature: {signature or '(unknown)'}\n"
            f"Snippet:\n{snippet}"
        )
        try:
            return self.llm_client.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                reasoning_effort="low",
                temperature=0.0,
            ).strip()
        except Exception:
            return ""

    # ---------- Mongo ----------

    def _bulk_upsert(self, docs: List[Dict[str, Any]]) -> None:
        if not docs:
            return
        ops = [
            {
                "update_one": {
                    "filter": {"id": d["id"], "type": "DEFINITIONS_RECORD"},
                    "update": {"$set": d},
                    "upsert": True,
                }
            }
            for d in docs
        ]
        try:
            self.collection.bulk_write(ops, ordered=False)
        except Exception:
            from pymongo.errors import DuplicateKeyError
            for d in docs:
                try:
                    self.collection.update_one(
                        {"id": d["id"], "type": "DEFINITIONS_RECORD"},
                        {"$set": d},
                        upsert=True,
                    )
                except DuplicateKeyError:
                    pass
