from collections import deque

from .db import get_mongo_client, get_neo4j_client, get_potential_entry_points, get_brief_file_overview, get_critical_file_paths
from .llm import GroqLLM
from .config import Config

MENTAL_MODEL_COL = "mental_model"

DOC_HUMAN = "REPO_ARCHITECTURE"
DOC_FILE_SUMMARY = "FILE_SUMMARY"


class RepoArchService:
    def __init__(self, repo_id: str):
        self.repo_id = repo_id
        self.mongo_client = get_mongo_client()
        self.neo4j_client = get_neo4j_client()

        self.mental = self.mongo_client[MENTAL_MODEL_COL]
        self.llm = GroqLLM()

    def get_repo_architecture(self) -> dict[str, str]:
        """
        Build architecture overviews by:
        - Selecting a minimal subset of entry points whose reachable files span the repo.
        - For each selected entry point:
          - Generating a brief, per-file summary for each reachable file.
          - Concatenating these summaries in breadth-first order from the entry point.
          - Chunking the concatenated summary into <=100k-character chunks and storing them.
        """
        entry_points = get_potential_entry_points(self.mongo_client, self.repo_id) or []

        self.create_repo_contxt(entry_points)
        # if not entry_points:
        #     raise ValueError(f"No entry points found for repo {self.repo_id}")

        # # Choose the smallest set of entry points that covers all reachable files
        # selected_entry_points = self._select_min_entry_points(entry_points)
        # print(f"Selected MIN entry points for repo {self.repo_id}: {selected_entry_points}")

        # results: dict[str, str] = {}
        # for ep in selected_entry_points:
        #     print(f"Creating repo overview for entry point: {ep}")
        #     summary = self.ep_analyse(entry_point=ep)
        #     results[ep] = summary

        # return results


    def create_repo_contxt(self, entry_points: list[str]):
        """
        Build REPO_CONTEXT by concatenating brief file overviews.

        - Visit each entry point in order.
        - For each entry point, do a BFS over its downstream dependencies.
        - For each critical file (has BRIEF_FILE_OVERVIEW) encountered for the first time:
        - Append its brief overview to the context (separated by two newlines).
        - Store the final concatenated string as a REPO_CONTEXT document in MongoDB.
        """
        if not entry_points:
            return []

        critical_files = set(get_critical_file_paths(self.mongo_client, self.repo_id))
        already_included: set[str] = set()
        context_parts: list[str] = []

        for ep in entry_points:
            # BFS from this entry point
            queue: deque[str] = deque()
            local_visited: set[str] = set()

            queue.append(ep)
            local_visited.add(ep)

            while queue:
                file_path = queue.popleft()

                # If this is a critical file and not yet included, append its brief overview
                if file_path in critical_files and file_path not in already_included:
                    brief = get_brief_file_overview(self.mongo_client, self.repo_id, file_path) or ""
                    if brief:
                        context_parts.append(brief)
                        already_included.add(file_path)

                # Traverse downstream dependencies
                cfi = self.neo4j_client.cross_file_interactions_in_file(
                    file_path=file_path,
                    repo_id=self.repo_id,
                )
                downstream_info = cfi.get("downstream", {}) or {}
                downstream_files = list(downstream_info.get("files", []) or [])

                for child_path in downstream_files:
                    if child_path not in local_visited:
                        local_visited.add(child_path)
                        queue.append(child_path)

        repo_context = "\n\n".join(context_parts)

        # Store as REPO_CONTEXT document
        doc = {
            "repo_id": self.repo_id,
            "document_type": "REPO_CONTEXT",
            "context": repo_context,
        }
        self.mental.update_one(
            {"repo_id": self.repo_id, "document_type": "REPO_CONTEXT"},
            {"$set": doc},
            upsert=True,
        )

        # Keep return type unchanged (if callers still expect a list of entry points)
        return entry_points


    def _reachable_files_from(self, entry_point: str) -> set[str]:
        """
        Return all files reachable from `entry_point` following downstream (DEPENDS_ON) edges.
        """
        visited: set[str] = set()
        queue: deque[str] = deque()

        visited.add(entry_point)
        queue.append(entry_point)

        while queue:
            file_path = queue.popleft()
            cfi = self.neo4j_client.cross_file_interactions_in_file(
                file_path=file_path,
                repo_id=self.repo_id,
            )
            downstream_info = cfi.get("downstream", {}) or {}
            downstream_files = list(downstream_info.get("files", []) or [])

            for child_path in downstream_files:
                if child_path not in visited:
                    visited.add(child_path)
                    queue.append(child_path)

        return visited


    def ep_analyse(self, entry_point: str) -> str:
        """
        For a given entry point:
        - Traverse the dependency graph in breadth-first order (downstream/DEPENDS_ON).
        - Ensure each file has a brief FILE_SUMMARY (LLM-generated, cached in Mongo).
        - Concatenate all per-file summaries in BFS order.
        - Chunk the concatenated text into <=100k-character chunks and store them
          in the REPO_ARCHITECTURE document, along with total_files.
        """
        # --- 1) BFS to collect reachable files in breadth-first order ---
        visited_files: set[str] = set()
        bfs_order: list[str] = []

        queue: deque[str] = deque()
        visited_files.add(entry_point)
        bfs_order.append(entry_point)
        queue.append(entry_point)

        while queue:
            file_path = queue.popleft()

            cfi = self.neo4j_client.cross_file_interactions_in_file(
                file_path=file_path,
                repo_id=self.repo_id,
            )
            downstream_info = cfi.get("downstream", {}) or {}
            downstream_files = list(downstream_info.get("files", []) or [])

            for child_path in downstream_files:
                if child_path not in visited_files:
                    visited_files.add(child_path)
                    bfs_order.append(child_path)
                    queue.append(child_path)

        total_files = len(visited_files)

        # --- 2) Ensure per-file summaries exist and build concatenated text ---
        lines: list[str] = []

        for file_path in bfs_order:
            # Try to reuse existing summary
            existing = self.mental.find_one({
                "repo_id": self.repo_id,
                "document_type": DOC_FILE_SUMMARY,
                "file_path": file_path,
            })

            if existing and existing.get("summary"):
                file_summary = existing["summary"]
            else:
                # Gather code for this file
                file_code_map = self.get_code_for_file([file_path])
                file_code = (file_code_map.get(file_path) or "").strip()

                # Get interactions for this file
                cfi = self.neo4j_client.cross_file_interactions_in_file(
                    file_path=file_path,
                    repo_id=self.repo_id,
                )
                downstream_info = cfi.get("downstream", {}) or {}
                upstream_info = cfi.get("upstream", {}) or {}

                downstream_interactions: list[str] = []
                for dep_file, interactions in (downstream_info.get("interactions", {}) or {}).items():
                    for inter in interactions or []:
                        downstream_interactions.append(f"{file_path} → {dep_file}: {inter}")

                upstream_interactions: list[str] = []
                for src_file, interactions in (upstream_info.get("interactions", {}) or {}).items():
                    for inter in interactions or []:
                        upstream_interactions.append(f"{src_file} → {file_path}: {inter}")

                file_summary = self.llm_summarize_file(
                    file_path=file_path,
                    file_code=file_code,
                    upstream_interactions=upstream_interactions,
                    downstream_interactions=downstream_interactions,
                )

                doc = {
                    "repo_id": self.repo_id,
                    "document_type": DOC_FILE_SUMMARY,
                    "file_path": file_path,
                    "summary": file_summary,
                }
                self.mental.update_one(
                    {
                        "repo_id": self.repo_id,
                        "document_type": DOC_FILE_SUMMARY,
                        "file_path": file_path,
                    },
                    {"$set": doc},
                    upsert=True,
                )

            # Append to concatenated architecture text
            lines.append(f"{file_path}: {file_summary}")

        architecture_text = "\n\n".join(lines)

        # --- 3) Chunk concatenated summary into <=100k-character chunks ---
        chunks = self._chunk_text(architecture_text, max_chars=100_000)

        # Persist the final overview as the human-facing architecture doc
        final_doc = {
            "repo_id": self.repo_id,
            "document_type": DOC_HUMAN,
            "entry_point": entry_point,
            "architecture_summary": architecture_text,
            "architecture_chunks": [
                {"index": idx, "text": chunk} for idx, chunk in enumerate(chunks)
            ],
            "total_files": total_files,
        }
        self.mental.update_one(
            {"repo_id": self.repo_id, "document_type": DOC_HUMAN, "entry_point": entry_point},
            {"$set": final_doc},
            upsert=True,
        )

        return architecture_text

    # -------------------------------------------------------------------------
    # LLM summarization for a single file (brief)
    # -------------------------------------------------------------------------

    def llm_summarize_file(
        self,
        *,
        file_path: str,
        file_code: str,
        upstream_interactions: list[str],
        downstream_interactions: list[str],
    ) -> str:
        """
        Create a brief, readable summary for a single file.

        Example shape:
        "`file_a.py` defines X and Y to do A, B, C, and interacts with `file_b.py` and `file_c.py` to ...".
        """
        upstream_block = "\n".join(f"- {i}" for i in (upstream_interactions or [])) or "—"
        downstream_block = "\n".join(f"- {i}" for i in (downstream_interactions or [])) or "—"

        system_prompt = """
            You are an expert engineer. Write a very brief, clear description of what a single source file does and how it fits into the codebase. 1–3 sentences, plain English, newcomer-friendly, no fluff.
            Mention:
            - main components (important functions/classes) and their purpose,
            - what the file is used for overall,
            - which other files it interacts with (if any).
            Use backticks for file paths and symbol names.
            """.strip()

        user_prompt = f"""
            You are summarizing one file.

            File path:
            `{file_path}`

            File code:
            {file_code}

            Upstream interactions (who calls this file and how):
            {upstream_block}

            Downstream interactions (who this file calls and how):
            {downstream_block}

            Write 1–3 sentences like:
            "`{file_path}` defines <key components> to <main responsibilities>, and interacts with <other files> to <reason>."

            Be concise but readable.
        """.strip()

        return self.llm.generate(
            model=Config.GPT_OSS_20GB_MODEL,
            system_prompt=system_prompt,
            prompt=user_prompt,
            reasoning_effort="medium",
        )

    # -------------------------------------------------------------------------
    # Code loading
    # -------------------------------------------------------------------------

    def get_code_for_file(self, file_paths: list[str]) -> dict[str, str]:
        """Fetch the full code content for each given file path from the local filesystem."""
        codes: dict[str, str] = {}
        for file_path in file_paths:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    code = f.read()
            except FileNotFoundError:
                code = ""
            codes[file_path] = code
        return codes

    # -------------------------------------------------------------------------
    # Chunking helper
    # -------------------------------------------------------------------------

    def _chunk_text(self, text: str, max_chars: int = 100_000) -> list[str]:
        """
        Split `text` into chunks of at most `max_chars` characters.
        """
        chunks: list[str] = []
        start = 0
        n = len(text)

        while start < n:
            end = min(start + max_chars, n)
            chunks.append(text[start:end])
            start = end

        return chunks


# === Convenience Functions =====================================================

async def build_repo_architecture_v2(repo_id: str) -> dict[str, str]:
    """Build architecture overviews using brief per-file summaries concatenated in BFS order."""
    builder = RepoArchService(repo_id=repo_id)
    return builder.get_repo_architecture()
