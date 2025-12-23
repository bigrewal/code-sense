import asyncio
import logging
from pathlib import Path
import traceback
from typing import Dict, List, Optional, Set, Tuple

from tqdm import tqdm

from ..db import Neo4jClient, get_mongo_client, get_neo4j_client
from ..llm_grok import GrokLLM
from ..repo_context_builder import build_repo_context
from ..models.data_model import IngestionJobStatus, IngestionStage, IngestionStageStatus

logger = logging.getLogger(__name__)


MENTAL_MODEL_TYPES = {
    "BRIEF": "BRIEF_FILE_OVERVIEW",
    "IGNORED": "IGNORED_FILE",
    "ENTRY": "POTENTIAL_ENTRY_POINTS",
}

PROMPT_SYSTEM = (
    "Your task is to analyze the provided code file and determine if it is critical to the core "
    "functionality of the repository. If the file is critical, write a 1–3 sentence plain-English "
    "description covering components, purpose, and key interactions. Otherwise, output IGNORE. "
    "Ignore tutorials, tests, and docs."
)

PROMPT_USER_TEMPLATE = """
Repo name:
{repo_id}

File path:
`{file_path}`

File code:
{code}

Upstream interactions:
{upstream}

Downstream interactions:
{downstream}

If critical, write 1–3 sentences like:
"`{file_path}` defines <key components> to <main responsibilities>, and interacts with <other files> to <reason>."
Otherwise, output "IGNORE".
Be concise but readable.
""".strip()


class MentalModelStage:
    """Stage for generating and storing the hierarchical mental model."""

    def __init__(self, llm_grok: GrokLLM, config: Optional[dict] = None):
        config = config or {}
        self.mongo_client = get_mongo_client()
        self.mental_model_collection = self.mongo_client["mental_model"]
        self.llm_client = llm_grok
        self.job_id = config.get("job_id", "unknown")
        self.batch_size = int(config.get("batch_size", 20))
        self.max_concurrency = int(config.get("max_concurrency", 10))

    async def run(self, repo_id: str, local_repo_path: Path):
        self.neo4j_client: Neo4jClient = get_neo4j_client()
        logger.info("Job %s: starting mental model generation for %s", self.job_id, repo_id)

        try:
            dir_tree = self._build_dir_tree(repo_id)
            insights, ignored_files = await self.identify_critical_files(dir_tree, repo_id)
            logger.info(
                "Job %s: generated overview with %s insights, ignored %s files",
                self.job_id,
                len(insights),
                len(ignored_files),
            )
            await self._set_potential_entry_points(insights, repo_id)
            repo_context_token_count = await build_repo_context(repo_id, self.llm_client)

            return len(insights), len(ignored_files), repo_context_token_count

        except Exception as e:
            logger.exception("Job %s: mental model generation error", self.job_id)
            traceback.print_exc()
            raise e

    async def _set_potential_entry_points(self, insights: List[dict], repo_id: str) -> List[str]:
        """Get potential entry points for the repo based on insights."""
        potential_entry_points = set()
        insights_files = {i.get("file_path") for i in insights if i.get("file_path")}

        for i in insights:
            fp = i.get("file_path")
            if fp:
                potential_entry_points.add(fp)

        for i in insights:
            fp = i.get("file_path")

            if not fp:
                continue
            ups = i.get("upstream_dep_files") or []
            for u in ups:
                if u in insights_files:
                    potential_entry_points.discard(fp)
                    break

        # Add potential entry points to the DB
        for file_path in potential_entry_points:
            document = {
                "repo_id": repo_id,
                "file_path": file_path,
                "document_type": MENTAL_MODEL_TYPES["ENTRY"],
            }
            self._upsert_document(document)

        return sorted(list(potential_entry_points))

    async def identify_critical_files(self, dir_tree: List[str], repo_id: str) -> Tuple[List[Dict[str, str]], Set[str]]:
        """Generate a comprehensive overview of the repo by summarizing critical files, ignoring non-critical ones."""

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def summarize_file(file_path: str) -> tuple[str, str]:
            cached = self.mental_model_collection.find_one(
                {
                    "repo_id": repo_id,
                    "file_path": file_path,
                    "document_type": {"$in": [MENTAL_MODEL_TYPES["BRIEF"], MENTAL_MODEL_TYPES["IGNORED"]]},
                },
                {"_id": 0, "data": 1},
            )
            if cached:
                return file_path, cached["data"]

            try:
                code = Path(file_path).read_text(encoding="utf-8")
            except Exception:
                logger.warning("Job %s: unable to read %s; marking ignored", self.job_id, file_path)
                return file_path, "IGNORE"

            cfi = self.neo4j_client.cross_file_interactions_in_file(
                file_path=file_path,
                repo_id=repo_id,
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

            upstream_block = "\n".join(f"- {i}" for i in (upstream_interactions or [])) or "—"
            downstream_block = "\n".join(f"- {i}" for i in (downstream_interactions or [])) or "—"

            user_prompt = PROMPT_USER_TEMPLATE.format(
                repo_id=repo_id,
                file_path=file_path,
                code=code,
                upstream=upstream_block,
                downstream=downstream_block,
            )

            async with semaphore:
                response = await self.llm_client.generate_async(
                    prompt=user_prompt,
                    system_prompt=PROMPT_SYSTEM,
                    temperature=0.0,
                )
            return file_path, response.strip()

        all_files = dir_tree

        insights: List[Dict[str, str]] = []
        ignored: Set[str] = set()

        pbar = tqdm(total=len(all_files), desc="Processing files")

        for i in range(0, len(all_files), self.batch_size):
            batch = all_files[i : i + self.batch_size]
            tasks = [summarize_file(fp) for fp in batch]
            results = await asyncio.gather(*tasks)

            for fp, summary in results:
                if summary == "IGNORE":
                    ignored.add(fp)
                    self._upsert_document(
                        {
                            "repo_id": repo_id,
                            "file_path": fp,
                            "document_type": MENTAL_MODEL_TYPES["IGNORED"],
                            "data": "IGNORE",
                        }
                    )
                else:
                    insights.append({"file_path": fp, "summary": summary})
                    self._upsert_document(
                        {
                            "repo_id": repo_id,
                            "file_path": fp,
                            "document_type": MENTAL_MODEL_TYPES["BRIEF"],
                            "data": summary,
                        }
                    )

            pbar.update(len(batch))

        pbar.close()

        for insight in insights:
            file_path = insight["file_path"]
            dependency_info = self._cross_file_interactions_in_file(file_path, repo_id)
            insight["downstream_dep_interactions"] = dependency_info["downstream"]["interactions"]
            insight["downstream_dep_files"] = list(dependency_info["downstream"]["files"])
            insight["upstream_dep_interactions"] = dependency_info["upstream"]["interactions"]
            insight["upstream_dep_files"] = list(dependency_info["upstream"]["files"])

        return insights, ignored

    def _build_dir_tree(self, repo_id: str) -> List[str]:
        """Build a nested dict representing the directory tree from file paths in Neo4j or MongoDB."""
        # Query Neo4j for all unique file_paths
        query = """
        MATCH (n:ASTNode {repo_id: $repo_id})
        RETURN DISTINCT n.file_path AS file_path
        """
        with self.neo4j_client.driver.session() as session:
            result = session.run(query, repo_id=repo_id)
            file_paths = [record["file_path"] for record in result]
        
        return file_paths

    def _cross_file_interactions_in_file(self, file_path: str, repo_id: str):
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

        with self.neo4j_client.driver.session() as session:
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

    def _upsert_document(self, document: Dict):
        """Persist a mental model document via upsert."""
        self.mental_model_collection.update_one(
            {
                "repo_id": document["repo_id"],
                "file_path": document["file_path"],
                "document_type": document["document_type"],
            },
            {"$set": document},
            upsert=True,
        )
