from .db import get_mongo_client, get_neo4j_client, get_potential_entry_points, get_brief_file_overviews
from .llm import GroqLLM

MENTAL_MODEL_COL = "mental_model"

DOC_HUMAN = "REPO_ARCHITECTURE"
DOC_ROLLING_SUMMARY = "ROLLING_SUMMARY"

class RepoArchService:
    def __init__(self, repo_id: str):
        self.repo_id = repo_id
        self.mongo_client = get_mongo_client()
        self.neo4j_client = get_neo4j_client()

        self.mental = self.mongo_client[MENTAL_MODEL_COL]
        self.llm = GroqLLM()

    def get_repo_architecture(self):
        entry_points = get_potential_entry_points(self.mongo_client, self.repo_id) or []
        if not entry_points:
            raise ValueError(f"No entry points found for repo {self.repo_id}")
        
        for ep in entry_points:
            print(f"Creating repo overview for entry point: {ep}")
            self.ep_analyse(entry_point=ep)


    def ep_analyse(self, entry_point: str) -> str:
        # ---------- 1) Build depth layers (BFS) ----------
        from collections import defaultdict, deque

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
            downstream_files = cfi["downstream"]["files"]
            # downstream_files may be a set; normalize to iteration-safe
            for dep in list(downstream_files):
                if dep not in visited:
                    visited.add(dep)
                    all_files.add(dep)
                    queue.append((dep, depth + 1))

        max_depth = max(files_by_depth.keys()) if files_by_depth else 0

        # ---------- 2) Iteratively merge per depth ----------
        rolling_summary = ""  # this accumulates the merged overview

        for depth in range(0, max_depth + 1):
            layer_files = files_by_depth.get(depth, [])
            if not layer_files:
                continue

            # Collect code for the current layer
            layer_code_map = self.get_code_for_file(layer_files)  # {path: code}

            # Aggregate observed interactions for this layer (restricted to discovered graph)
            downstream_interactions = []
            upstream_interactions = []

            for path in layer_files:
                cfi = self.neo4j_client.cross_file_interactions_in_file(
                    file_path=path,
                    repo_id=self.repo_id
                )

                # Only keep interactions that involve files we actually discovered (all_files)
                for fname, interactions in cfi.get("downstream", {}).get("interactions", {}).items():
                    if fname in all_files:
                        downstream_interactions.extend(interactions)

                for fname, interactions in cfi.get("upstream", {}).get("interactions", {}).items():
                    if fname in all_files:
                        upstream_interactions.extend(interactions)

            # Check for an existing cached rolling summary at this depth (resume capability)
            existing = self.mental.find_one({
                "repo_id": self.repo_id,
                "document_type": DOC_ROLLING_SUMMARY,
                "entry_point": entry_point,
                "depth": depth
            })

            if existing and existing.get("rolling_summary"):
                rolling_summary = existing["rolling_summary"]
            else:
                # ---------- LLM merge step ----------
                # NOTE: llm_summarize will be updated next to accept these arguments
                rolling_summary = self.llm_summarize(
                    previous_summary=rolling_summary,            # merged overview so far (string)
                    entry_point=entry_point,                     # hinge for coherence
                    depth=depth,                                  # current depth
                    layer_file_paths=layer_files,                # list[str]
                    layer_file_codes=layer_code_map,             # dict[str, str]
                    downstream_interactions=downstream_interactions,
                    upstream_interactions=upstream_interactions
                )

                # Persist per-depth rolling summary for resumability
                key = {
                    "repo_id": self.repo_id,
                    "document_type": DOC_ROLLING_SUMMARY,
                    "entry_point": entry_point,
                    "depth": depth,
                    "rolling_summary": rolling_summary
                }
                self.mental.update_one(key, {"$set": key}, upsert=True)

        # ---------- 3) Persist the final merged overview ----------
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


    def llm_summarize(
        self,
        *,
        previous_summary: str,
        entry_point: str,
        depth: int,
        layer_file_paths: list[str],
        layer_file_codes: dict[str, str],
        downstream_interactions: list[str],
        upstream_interactions: list[str],
    ) -> str:
        """
        Rolling merge summarizer:
        - Takes the existing merged overview (previous_summary)
        - Integrates the current depth's files + interactions
        - Returns a single, coherent, comprehensive overview

        Keep this SIMPLE and iterative: each call expands/refines without losing detail.
        """

        # Build compact, deterministic blocks for the new layer
        files_block = "\n\n".join(
            f"- `{p}`\n```\n{(layer_file_codes.get(p) or '').strip()}\n```"
            for p in layer_file_paths
        ) or "—"

        downstream_block = "\n".join(f"- {i}" for i in (downstream_interactions or [])) or "—"
        upstream_block = "\n".join(f"- {i}" for i in (upstream_interactions or [])) or "—"

        system_prompt = """
        You are an expert software engineer and technical writer. 
        Your goal is to create a clear, accurate, and easy-to-read architecture overview 
        that helps a newcomer quickly understand how this repository works.

        CRITICAL REQUIREMENTS
        - Audience: developers new to the repository who need to understand its structure and flow.
        - Focus on clarity, coherence, and practical understanding.
        - Style: neutral, explanatory, and instructional. Avoid unnecessary jargon or repetition.
        - Always include file names and component names (functions, classes, methods) in backticks.
        - Do NOT list code line by line or describe syntax.
        - Do NOT use headings like "Depth" or mention how the summary was built.
        - Keep the explanation technically accurate and logically organized.
        - Preserve all important details from earlier summaries; integrate new ones naturally.
        - If information is missing, use an em dash (—).

        OVERALL STRUCTURE (keep this stable in every iteration):

        # Repository Architecture Overview

        ## Overview
        Start with a few paragraphs describing what this repository or system does overall, 
        its main purpose, and the core problem it solves. 
        Then, give a brief sense of how the different parts fit together.

        ## Key Components
        List and describe the major files, modules, classes, and functions. 
        For each, explain what it does and how it fits into the broader system.
        Be concise but specific — what data it handles, what tasks it performs, and how it interacts with other components.
        Reference other files or functions by name (wrapped in backticks).

        ## Control Flow and Interactions
        Explain how control and data move through the system in a logical order.
        Use directional arrows (→) to show who calls whom and for what purpose.
        Example:
        `main.py` → `parser.py` — parses configuration and input data.
        `parser.py` → `executor.py` — runs tasks based on parsed instructions.
        Focus on the "story" of execution from start to finish — what happens first, what follows, and why.

        ## Data and External Systems
        Describe how the repository interacts with databases, APIs, configuration files, message queues, or external tools.
        Clarify which modules handle these interactions and how data moves between them.

        ## Design Patterns and Principles
        Briefly mention any architectural patterns or design choices (e.g., visitor pattern, MVC, pipeline).
        Explain why they exist and how they help organize the system.

        ## Error Handling and Edge Cases
        Highlight any notable error handling strategies, recovery mechanisms, or constraints.

        ## Summary
        End with a short recap that ties everything together — 
        what the repo accomplishes, how its pieces collaborate, and what a new contributor should explore first to gain deeper understanding.

        Your task:
        - Merge new information naturally into this structure.
        - Expand and clarify the existing understanding without repeating or compressing it.
        - The final output should feel like a well-written walkthrough of how the repository works.
        """.strip()

        
        user_prompt = f"""
            Here is the current architecture overview (if any). 
            Preserve all important details and integrate new information naturally:
            {previous_summary or "(none yet)"}

            New source material to integrate:
            Files and their code:
            {files_block}

            Observed interactions involving these files:
            - Upstream (callers):
            {upstream_block}

            - Downstream (callees):
            {downstream_block}

            Your task:
            - Expand and refine the architecture overview using the structure described in the system prompt.
            - Do NOT remove existing useful information — integrate and clarify it.
            - Add new explanations for the files, components, and interactions provided above.
            - Keep the final document coherent, accurate, and easy to follow for newcomers.
            - Mention file paths and component names in backticks.
            - Maintain the existing section structure (Overview, Key Components, Control Flow, etc.).
            - Write in a natural, explanatory tone that feels like a guided tour of how the repository works.
            - The entry point is `{entry_point}` — keep it as the conceptual anchor of the explanation.
            """.strip()
        
        return self.llm.generate(
            system_prompt=system_prompt,
            prompt=user_prompt,
            reasoning_effort="medium",
        )


    def get_code_for_file(self, file_paths: list[str]) -> dict[str, str]:
        """Fetch the full code content for a given file from MongoDB."""

        codes = {}
        for file_path in file_paths:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            codes[file_path] = code

        return codes

# === Convenience Functions =====================================================

async def build_repo_architecture_v2(repo_id: str) -> dict[str, any]:
    """Build architecture overviews using bottom-up component cards."""
    builder = RepoArchService(repo_id=repo_id)
    return builder.get_repo_architecture()