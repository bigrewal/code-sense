from collections import deque

from .db import get_mongo_client, get_neo4j_client, MyMongoClient, Neo4jClient
from .llm import GroqLLM
from .config import Config

MENTAL_MODEL_COL = "mental_model"

DOC_HUMAN = "REPO_ARCHITECTURE"
DOC_FILE_SUMMARY = "FILE_SUMMARY"


class RepoArchService:
    def __init__(self, repo_id: str):
        self.repo_id = repo_id
        self.mongo_client: MyMongoClient = get_mongo_client()
        self.neo4j_client: Neo4jClient = get_neo4j_client()

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
        entry_points = self.mongo_client.get_potential_entry_points(self.repo_id) or []

        self.create_repo_contxt(entry_points)


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

        critical_files = set(self.mongo_client.get_critical_file_paths(self.repo_id))
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
                    brief = self.mongo_client.get_brief_file_overview(self.repo_id, file_path) or ""
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


# === Convenience Functions =====================================================

async def build_repo_architecture_v2(repo_id: str) -> dict[str, str]:
    """Build architecture overviews using brief per-file summaries concatenated in BFS order."""
    builder = RepoArchService(repo_id=repo_id)
    return builder.get_repo_architecture()