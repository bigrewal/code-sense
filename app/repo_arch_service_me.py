from .db import get_mongo_client, get_neo4j_client, get_potential_entry_points, get_brief_file_overviews
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
        Build architecture overviews by doing a bottom-up DFS from each entry point.

        For each file:
        - If a FILE_SUMMARY exists in MongoDB, reuse it.
        - Otherwise, recursively summarize children (downstream dependencies) first,
          then create this file's summary by merging its own behaviour with the
          summaries of its immediate children and its upstream/downstream interactions.

        The REPO_ARCHITECTURE document for an entry point is simply the summary of
        the entry point file (which itself incorporates its subtree).
        """
        entry_points = get_potential_entry_points(self.mongo_client, self.repo_id) or []
        if not entry_points:
            raise ValueError(f"No entry points found for repo {self.repo_id}")

        results: dict[str, str] = {}

        for ep in entry_points:
            print(f"Creating repo overview for entry point: {ep}")
            summary = self.ep_analyse(entry_point=ep)
            results[ep] = summary

        return results

    def ep_analyse(self, entry_point: str) -> str:
        """
        Build (or reuse) a bottom-up summary rooted at `entry_point`.

        This uses DFS to ensure children are summarized before parents.
        """
        visited: set[str] = set()
        visiting: set[str] = set()

        entry_summary = self._summarize_file_dfs(
            file_path=entry_point,
            visited=visited,
            visiting=visiting,
        )

        total_files = len(visited)

        # Persist the final merged overview as the human-facing architecture doc
        final_doc = {
            "repo_id": self.repo_id,
            "document_type": DOC_HUMAN,
            "entry_point": entry_point,
            "architecture_summary": entry_summary,
            "total_files": total_files,
        }
        self.mental.update_one(
            {"repo_id": self.repo_id, "document_type": DOC_HUMAN, "entry_point": entry_point},
            {"$set": final_doc},
            upsert=True,
        )

        return entry_summary

    # -------------------------------------------------------------------------
    # DFS + per-file summarization
    # -------------------------------------------------------------------------

    def _summarize_file_dfs(
        self,
        file_path: str,
        visited: set[str],
        visiting: set[str],
    ) -> str:
        """
        DFS helper that ensures we summarize children before parents.

        For a given file:
        1. DFS into downstream dependencies (children) first.
        2. Reuse existing FILE_SUMMARY from MongoDB if present.
        3. Otherwise, call the LLM to create a new summary, merging:
           - This file's own code
           - Summaries of its immediate children
           - Upstream and downstream interactions
        """
        # If we already fully processed this file, reuse its summary from Mongo (if any)
        if file_path in visited:
            existing = self.mental.find_one({
                "repo_id": self.repo_id,
                "document_type": DOC_FILE_SUMMARY,
                "file_path": file_path,
            })
            if existing and existing.get("summary"):
                return existing["summary"]
            # Fall through and recompute if somehow visited but no summary stored

        # Cycle protection
        if file_path in visiting:
            # We avoid infinite recursion; give a minimal note so the parent still gets something.
            return f"A cycle was detected involving `{file_path}`. This file participates in a circular dependency; see its callers and callees in the graph for details."

        visiting.add(file_path)

        # Get cross-file interactions for this file
        cfi = self.neo4j_client.cross_file_interactions_in_file(
            file_path=file_path,
            repo_id=self.repo_id,
        )
        downstream_info = cfi.get("downstream", {}) or {}
        upstream_info = cfi.get("upstream", {}) or {}

        downstream_files = list(downstream_info.get("files", []) or [])

        # First, summarize all children (downstream dependencies)
        child_summaries: dict[str, str] = {}
        for child_path in downstream_files:
            child_summary = self._summarize_file_dfs(child_path, visited, visiting)
            if child_summary:
                child_summaries[child_path] = child_summary

        visiting.remove(file_path)

        # Check if a summary for this file already exists
        existing = self.mental.find_one({
            "repo_id": self.repo_id,
            "document_type": DOC_FILE_SUMMARY,
            "file_path": file_path,
        })

        if existing and existing.get("summary"):
            summary = existing["summary"]
        else:
            # Gather code for this file
            file_code_map = self.get_code_for_file([file_path])
            file_code = (file_code_map.get(file_path) or "").strip()

            # Flatten interactions into simple text lists for the prompt
            downstream_interactions: list[str] = []
            for dep_file, interactions in (downstream_info.get("interactions", {}) or {}).items():
                for inter in interactions or []:
                    downstream_interactions.append(f"{file_path} → {dep_file}: {inter}")

            upstream_interactions: list[str] = []
            for src_file, interactions in (upstream_info.get("interactions", {}) or {}).items():
                for inter in interactions or []:
                    upstream_interactions.append(f"{src_file} → {file_path}: {inter}")

            summary = self.llm_summarize_file(
                file_path=file_path,
                file_code=file_code,
                child_summaries=child_summaries,
                upstream_interactions=upstream_interactions,
                downstream_interactions=downstream_interactions,
            )

            doc = {
                "repo_id": self.repo_id,
                "document_type": DOC_FILE_SUMMARY,
                "file_path": file_path,
                "summary": summary,
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

        visited.add(file_path)
        return summary

    # -------------------------------------------------------------------------
    # LLM summarization for a single file (bottom-up)
    # -------------------------------------------------------------------------

    def llm_summarize_file(
        self,
        *,
        file_path: str,
        file_code: str,
        child_summaries: dict[str, str],
        upstream_interactions: list[str],
        downstream_interactions: list[str],
    ) -> str:
        """
        Create a dense but readable summary for a single file, using a fixed structure:

        - Overall purpose: what this file is responsible for, in the context of the system.
        - How it works (Step 1, Step 2, ...): a clear, newcomer-friendly walkthrough.
        - Final output / effect: what comes out of this file (side effects, return values, state changes).

        The summary:
        - Should reference functions/methods and the file path using backticks (e.g. `process_payment` in `payments/service.py`).
        - Should explain what the parent uses its children for AND what upstream callers use this file for.
        - Should be information-dense but not cryptic; it must stay easy to read.
        """
        children_block = "\n\n".join(
            f"Child file: `{child_path}`\nExisting summary:\n{summary}"
            for child_path, summary in (child_summaries or {}).items()
        ) or "—"

        upstream_block = "\n".join(f"- {i}" for i in (upstream_interactions or [])) or "—"
        downstream_block = "\n".join(f"- {i}" for i in (downstream_interactions or [])) or "—"

        system_prompt = """
            You are an expert software engineer and technical writer.

            Your task is to produce a clear and newcomer-friendly explanation
            of how a **single source file** works AND how the entire subtree of files
            beneath it works.

            This summary may eventually represent the *entire repository architecture*
            when the root entry point is summarized, so your output must integrate and
            compress all relevant behaviour from the file’s direct children and their
            children (through their summaries).

            ---------------------------------------------------------------------------
            CRITICAL REQUIREMENTS
            ---------------------------------------------------------------------------

            AUDIENCE
            - Developers new to the repository who need to understand how the system
            works end-to-end by reading *only this summary*.

            OVERALL GOAL
            - Each parent summary must **merge** and **absorb** the important behaviour of
            its children.
            - A reader should NOT need to read the child summaries separately; all
            essential behaviour must be pulled upward and restated in this file’s
            summary.

            STYLE
            - Concise but NOT cryptic. Use short, complete sentences and small paragraphs.
            - High information density without reducing readability.
            - Don't miss any important behaviour in parents or children.
            - Avoid repetition, filler, or listing code line-by-line.
            - Focus entirely on behaviour, flow, and responsibilities.
            - Always refer to functions, methods, classes, and files using backticks.

            ---------------------------------------------------------------------------
            WHAT “MERGE” MEANS — VERY IMPORTANT
            ---------------------------------------------------------------------------

            When this file depends on child files:

            1. **Pull essential behaviour upward**
            - For each child, extract the key responsibilities, major steps, and
                important logic described in its summary.
            - Integrate these into THIS file’s explanation of how it works.
            - Do not merely reference the child — *explain what it does*.
            - Don't miss any important behaviour; the goal is to make the entire subtree
                understandable from this summary alone.

            2. **Explain what the parent uses each child for**
            - Be explicit about HOW and WHY this file calls into each child.
            - Describe the role each child plays in the overall flow.

            3. **Explain what upstream callers rely on this file for**
            - What service does this file provide to its callers?
            - What data or side effects does it produce for them?

            4. **If this file is an entry point**, your summary becomes the
            **repository architecture overview**.
            - Give a clear, coherent, top-down explanation of the entire system.
            - Include all significant behaviours drawn from the full subtree.

            ---------------------------------------------------------------------------
            STRUCTURE (use this exact structure for every file)
            ---------------------------------------------------------------------------

            # File Overview

            ## Overall Purpose
            - 1–2 paragraphs describing what this file (`<file_path>`) is responsible for.
            - Summarize how it fits into the system and how its children contribute to its job.
            - Integrate high-level behaviour from child summaries where relevant.

            ## How It Works (Step by Step)
            - A numbered list: Step 1, Step 2, Step 3, …
            - Describe the main flow of this file AND key steps happening inside children.
            Examples:
            - “The `run` function in `<file>` calls `parse_payload` in `utils/parser.py`
                to validate the incoming data…”
            - “Next, it delegates business logic to `process_invoice` in `billing/core.py`,
                which performs …”
            - Ensure the reader can understand the entire workflow from this summary alone.

            ## Final Output and Effects
            - Describe the data or side effects produced by this file and its subtree.
            - Explain what upstream callers rely on it for.
            - Summarize the practical outcome of the entire flow.

            ---------------------------------------------------------------------------
            TONE AND DENSITY
            ---------------------------------------------------------------------------
            - High information density, minimal fluff.
            - But maintain readability and natural flow.
            - The final document should feel like a guided tour of how this part of the
            system works, not a compressed index.
        """.strip()

        user_prompt = f"""
            You are summarizing a single file in a repository.

            Your output MUST integrate both:
            1. The behaviour of this file itself, and  
            2. The essential behaviour of all its immediate children (downstream dependencies),  
            whose summaries are provided below.

            Do NOT assume the reader will ever look at the children’s summaries.  
            Your summary must fully absorb their important responsibilities and restate them
            inside this file’s own explanation.

            ---------------------------------------------------------------------------
            FILE BEING SUMMARIZED
            ---------------------------------------------------------------------------
            File path:
            `{file_path}`

            File code:
            ```
            {file_code}   
            ```
            ---------------------------------------------------------------------------
            CHILD FILE SUMMARIES (MUST BE MERGED INTO THE PARENT SUMMARY)
            ---------------------------------------------------------------------------
            Below are the existing summaries of this file’s immediate children.
            You MUST pull their essential behaviour upward and integrate it naturally
            into this file’s “Overall Purpose” and “How It Works” explanations.

            {children_block}

            ---------------------------------------------------------------------------
            OBSERVED INTERACTIONS
            ---------------------------------------------------------------------------
            Upstream interactions (who calls this file and how):
            {upstream_block}

            Downstream interactions (how this file calls others):
            {downstream_block}

            ---------------------------------------------------------------------------
            YOUR TASK
            ---------------------------------------------------------------------------

            Produce a full, self-contained summary for `{file_path}` using the exact
            section structure defined in the system prompt.

            MANDATORY REQUIREMENTS:

            1. **Merging child behaviour**
            - Extract the key ideas, responsibilities, and steps from each child
                summary and incorporate them directly into your explanation of this file.
            - Do NOT merely reference children — *explain what they do* and how
                they fit into this file’s workflow.
            - By the end, this summary must make the entire subtree understandable
                even without reading the child summaries.

            2. **Explain how the parent uses its children**
            - Be explicit about why this file calls each child and what the child
                contributes to the overall flow.

            3. **Explain what upstream callers rely on this file for**
            - Clarify what capability this file provides to others.

            4. **Use code pointers cleanly**
            - Refer to important functions, classes, and methods like:
                “The `process_event` function in `{file_path}`…”
            - Keep the writing readable — not cluttered.

            5. **Tone & clarity**
            - High information density, but still easy for a newcomer to read.
            - Clear paragraphs and a numbered “How It Works” section.
            - Avoid low-level syntax descriptions and avoid repetition.

            When you’re done, the reader should understand the behaviour of:
            - this file,
            - its immediate children,
            - and the entire subtree rooted here —
            from this single summary alone.

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


# === Convenience Functions =====================================================

async def build_repo_architecture_v2(repo_id: str) -> dict[str, str]:
    """Build architecture overviews using bottom-up per-file summaries."""
    builder = RepoArchService(repo_id=repo_id)
    return builder.get_repo_architecture()
