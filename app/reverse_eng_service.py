import hashlib
from collections import defaultdict, deque
import asyncio

from .db import get_mongo_client, get_neo4j_client, get_potential_entry_points, get_brief_file_overviews
from .llm import GroqLLM
from .config import Config

MENTAL_MODEL_COL = "mental_model"

DOC_HUMAN = "REVERSE_ENG"
DOC_ROLLING_SUMMARY = "ROLLING_REV_ENG_SUMMARY"

class ReverseEngService:
    def __init__(self, repo_id: str):
        self.repo_id = repo_id
        self.mongo_client = get_mongo_client()
        self.neo4j_client = get_neo4j_client()

        self.mental = self.mongo_client[MENTAL_MODEL_COL]
        self.llm = GroqLLM()

    async def get_reverse_eng(self):
        entry_points = get_potential_entry_points(self.mongo_client, self.repo_id) or []
        if not entry_points:
            raise ValueError(f"No entry points found for repo {self.repo_id}")
        
        for ep in entry_points:
            await self.ep_analyse(entry_point=ep)
            print(f"Completed repo reverse engineering for entry point: {ep}")
        
        print("All entry points processed for reverse engineering.")

    async def ep_analyse(self, entry_point: str) -> str:
        # ---------- 1) Build depth layers (BFS by file) ----------
        files_by_depth = defaultdict(list)
        visited = set([entry_point])
        all_files = set([entry_point])
        queue = deque([(entry_point, 0)])

        while queue:
            file_path, depth = queue.popleft()
            files_by_depth[depth].append(file_path)

            cfi = self.neo4j_client.cross_file_interactions_in_file(
                file_path=file_path,
                repo_id=self.repo_id
            )
            downstream_files = cfi.get("downstream", {}).get("files", set()) or set()
            for dep in list(downstream_files):
                if dep not in visited:
                    visited.add(dep)
                    all_files.add(dep)
                    queue.append((dep, depth + 1))

        max_depth = max(files_by_depth.keys()) if files_by_depth else 0

        # ---------- 2) Rolling spec (Markdown) ----------
        def ensure_base(markdown: str) -> str:
            if not markdown or "# Repository Technical Spec (v0.1)" not in markdown:
                return "# Repository Technical Spec (v0.1)\n\n## Files\n"
            return markdown

        def read_code(path: str) -> str:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        def control_flow_bullets(src_path: str, cfi_dict: dict) -> list[str]:
            bullets = []
            for fname in sorted(cfi_dict.get("downstream", {}).get("files", set()) or set()):
                bullets.append(f"- `{src_path}` → `{fname}`")
            return bullets

        def upsert_section(markdown: str, heading: str, lines: list[str]) -> str:
            """Ensure a heading exists once; dedup lines by exact match, keep order stable."""
            if not lines:
                return markdown
            if heading not in markdown:
                markdown += f"\n\n{heading}\n"
            before, sep, after = markdown.partition(heading)
            if not sep:
                return markdown  # race guard
            existing = set()
            body_lines = []
            for ln in after.splitlines():
                if ln.startswith("## "):  # next section
                    break
                if ln.strip():
                    existing.add(ln.strip())
                    body_lines.append(ln)
            # dedup + append
            new = []
            for ln in lines:
                if ln.strip() not in existing:
                    new.append(ln)
                    existing.add(ln.strip())

            # rebuild: keep original after, but replace within this section
            # find where this section ends
            after_lines = after.splitlines()
            end_idx = 0
            for i, ln in enumerate(after_lines):
                if i > 0 and ln.startswith("## "):
                    end_idx = i
                    break
            section_body = "\n".join([*body_lines, *new]).strip()
            rebuilt = f"{before}{heading}\n{section_body}\n"
            if end_idx:
                rebuilt += "\n" + "\n".join(after_lines[end_idx:]) + "\n"
            return rebuilt

        def upsert_file_block(markdown: str, path: str, block: str) -> str:
            """Replace file block if HASH changed; insert if missing. Block must start with '### `path`'."""
            markdown = ensure_base(markdown)
            files_heading = "## Files"
            if files_heading not in markdown:
                markdown += f"\n\n{files_heading}\n"

            # find existing block start
            start_marker = f"### `{path}`"
            if start_marker not in markdown:
                # insert at end of Files section, before next '## ' heading
                before, sep, after = markdown.partition(files_heading)
                rest = after.splitlines()
                insert_at = len(rest)
                for i, ln in enumerate(rest[1:], start=1):
                    if ln.startswith("## "):  # next top-level section
                        insert_at = i
                        break
                new_after = "\n".join(rest[:insert_at] + [block] + rest[insert_at:])
                return before + sep + new_after

            # replace the whole existing block
            before, sep, after = markdown.partition(start_marker)
            # reconstruct the start line fully (we split at first match)
            after = start_marker + after
            lines = after.splitlines()
            # consume until next file heading or next '## ' section
            cut = 0
            for i, ln in enumerate(lines[1:], start=1):
                if ln.startswith("### `") or ln.startswith("## "):
                    cut = i
                    break
            remaining = "\n".join(lines[cut:]) if cut else ""
            return before + block + ("\n" + remaining if remaining else "\n")

        # ---------- 3) Iterate layers; per-file LLM, then merge ----------
        rolling_summary = ""
        for depth in range(0, max_depth + 1):
            layer_files = files_by_depth.get(depth, [])
            if not layer_files:
                continue

            # Respect resumability
            existing = self.mental.find_one({
                "repo_id": self.repo_id,
                "document_type": DOC_ROLLING_SUMMARY,
                "entry_point": entry_point,
                "depth": depth
            })
            if existing and existing.get("rolling_summary"):
                rolling_summary = existing["rolling_summary"]

            rolling_summary = ensure_base(rolling_summary)

            # compute all cfi once per file to reuse for CF bullets
            per_file_cfi = {}
            for path in layer_files:
                per_file_cfi[path] = self.neo4j_client.cross_file_interactions_in_file(
                    file_path=path, repo_id=self.repo_id
                )

            # ---- N per-file requests (sequential for simplicity) ----
            tasks = []
            for path in layer_files:
                try:
                    code = read_code(path)
                except Exception:
                    code = ""

                upstream = sorted(list(per_file_cfi[path].get("upstream", {}).get("files", set()) or set()))
                downstream = sorted(list(per_file_cfi[path].get("downstream", {}).get("files", set()) or set()))
                # only keep interactions within discovered graph to avoid noise
                upstream = [p for p in upstream if p in all_files]
                downstream = [p for p in downstream if p in all_files]

                # single-file SpecMark block
                tasks.append(
                    self.llm_summarize(
                        entry_point=entry_point,
                        depth=depth,
                        file_path=path,
                        file_code=code,
                        upstream_paths=upstream,
                        downstream_paths=downstream,
                        previous_doc=rolling_summary,
                    )
                )
            
            blocks = await asyncio.gather(*tasks)
            for path, block in zip(layer_files, blocks):
                rolling_summary = upsert_file_block(rolling_summary, path, block.strip() + "\n")

            # ---- Merge control-flow bullets for this layer (dedup) ----
            cf_lines = []
            for src in layer_files:
                cf_lines.extend(control_flow_bullets(src, per_file_cfi[src]))
            rolling_summary = upsert_section(rolling_summary, "## Control Flow", sorted(set(cf_lines)))

            # Persist per-depth
            key = {
                "repo_id": self.repo_id,
                "document_type": DOC_ROLLING_SUMMARY,
                "entry_point": entry_point,
                "depth": depth,
                "rolling_summary": rolling_summary
            }
            self.mental.update_one(key, {"$set": key}, upsert=True)

        # ---------- 4) Persist final ----------
        final_doc = {
            "repo_id": self.repo_id,
            "document_type": DOC_HUMAN,
            "entry_point": entry_point,
            "architecture_summary": rolling_summary
        }
        self.mental.update_one(
            {"repo_id": self.repo_id, "document_type": DOC_HUMAN, "entry_point": entry_point},
            {"$set": final_doc},
            upsert=True
        )
        return rolling_summary

    
    async def llm_summarize(
        self,
        *,
        entry_point: str,
        depth: int,
        file_path: str,
        file_code: str,
        upstream_paths: list[str],
        downstream_paths: list[str],
        previous_doc: str,
    ) -> str:
        """
        Return a **single-file** SpecMark block. Must either create a new block or
        replace the existing one for this file. No repo-wide edits here.
        The block MUST start with:
        ### `path/to/file` 
        """
        upstream_block = ", ".join(f"`{p}`" for p in upstream_paths) or "—"
        downstream_block = ", ".join(f"`{p}`" for p in downstream_paths) or "—"

        system_prompt = f"""
            You are an expert software engineer. 
            Your task is to reverse-engineer the provided source code and produce a SINGLE Markdown block 
            that fully explains this file’s behaviour so that another engineer could faithfully re-implement it.

            STRICT RULES:
            - Output ONLY the block for this file; do NOT include any other sections or headings.
            - START the block with: ### `{file_path}`
            - Use these exact field labels if applicable; omit lines that do not apply: PURPOSE, EXPORTS, IMPORTS, API, ALGORITHM, STATE, DATAFLOW, EXTERNAL, CONFIG, INVARIANTS, ASSUMPTIONS, PROVENANCE.
            - Describe behaviour, responsibilities, data flow, and logic clearly enough that the file can be re-implemented without the original code.
            - Provide pseudocode for ALGORITHM when meaningful.
            - Mention other files only under IMPORTS or where directly relevant; do not describe their internals.
            - Deterministic tone; no speculation. If unknown, omit the line.
            - Do NOT restate or modify any other file’s content from the previous doc.  
                """.strip()

        user_prompt = f"""
            Previous doc (for anchors only; do NOT edit it directly):

            {previous_doc or ''}

            Now generate the block for this file ONLY.

            File path: `{file_path}`
            Entry point (for context): `{entry_point}`
            Depth: {depth}

            Upstream (callers): {upstream_block}
            Downstream (callees): {downstream_block}

            File code:
            {file_code or ''}
        
            Remember:
            - Output ONLY the block for `{file_path}`.
            - Begin with: ### `{file_path}`
            - Omit empty fields entirely.
            - Keep API signatures and ALGORITHM pseudocode tight and actionable.
            - End the block with a trailing newline.
                """.strip()

        return await self.llm.generate_async(
            system_prompt=system_prompt,
            prompt=user_prompt,
            reasoning_effort="medium",
            temperature=0,
            model=Config.GPT_OSS_120GB_MODEL,
        )


# === Convenience Functions =====================================================

async def reverse_engineer(repo_id: str) -> dict[str, any]:
    """Build architecture overviews using bottom-up component cards."""
    builder = ReverseEngService(repo_id=repo_id)
    return await builder.get_reverse_eng()