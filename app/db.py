import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from bson import ObjectId
from neo4j import GraphDatabase
from pymongo import MongoClient

from .config import Config
from .utils import now_ts
from .models.data_model import ASTNode, IngestionJobStatus, IngestionStage
logger = logging.getLogger(__name__)

## Silence WARNING:neo4j.notifications: 
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)

INGESTION_JOBS_COLLECTION = "ingestion_jobs"
INGESTED_REPOS_COLLECTION = "ingested_repos"
CONVERSATIONS_COLLECTION = "conversations"
MESSAGES_COLLECTION = "messages"
MENTAL_MODEL_COLLECTION = "mental_model"


def _serialize_job(job_doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job_doc.get("job_id"),
        "repo_name": job_doc.get("repo_name"),
        "status": job_doc.get("status"),
        "current_stage": job_doc.get("current_stage"),
        "stages": job_doc.get("stages", {}),
        "error": job_doc.get("error"),
        "created_at": job_doc.get("created_at"),
        "updated_at": job_doc.get("updated_at"),
    }

def _filter_stage_metrics(job: Dict[str, Any]) -> Dict[str, Any]:
    ALLOWED_PRECHECK_METRICS = {
        "total_tokens",
        "supported_tokens",
        "supported_ratio",
        "primary_language",
        "language_distribution_pct",
        "supported_file_count",
        "unsupported_file_count",
        "excluded_file_count",
    }
    stages = job.get("stages") or {}

    pre = stages.get(IngestionStage.PRECHECK.value)
    if pre and "metrics" in pre:
        pre["metrics"] = {
            k: v for k, v in pre["metrics"].items()
            if k in ALLOWED_PRECHECK_METRICS
        }

    return job

class Neo4jClient:
    """Neo4j database client wrapper."""
    _instance = None  # Singleton instance

    def __new__(cls, *args, **kwargs):
        # Enforce singleton
        if cls._instance is None:
            cls._instance = super(Neo4jClient, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # Prevent re-initialisation on subsequent instantiations
        if getattr(self, "_initialized", False):
            return

        self.driver = None
        self.batch_size = 1000
        logger.info(f"Initializing Neo4jClient with config: {Config.NEO4J_URI}")
        try:
            self.driver = GraphDatabase.driver(
                Config.NEO4J_URI,
                auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD)
            )
            # Test connection
            with self.driver.session() as session:
                session.run("RETURN 1")
            logger.info("Neo4j connection established successfully")
        except Exception as e:
            logger.warning(f"Neo4j connection failed: {e}")
            raise

        self._initialized = True

    async def init_graph_for_repo(self, repo_id: str):
        if not self.driver:
            logger.error("Neo4j driver not initialized")
            raise Exception("Neo4j driver not initialized")

        async def _initialise():
            try:
                logger.info("Initialising graph for repo: %s", repo_id)
                with self.driver.session() as session:
                    self._create_indexes(session)
                    self.clear_repo_data(session, repo_id)
            except Exception as exc:
                logger.error("Neo4j initialisation failed: %s", exc)
                raise

        await asyncio.to_thread(_initialise)

    def _create_indexes(self, session):
        """Create necessary indexes for performance."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.node_id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.repo_id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.file_path)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.node_type)",
        ]

        for index_query in indexes:
            try:
                session.run(index_query)
                # logger.debug(f"Created index: {index_query}")
            except Exception as e:
                logger.warning(f"Index creation failed: {e}")

    def clear_repo_data(self, session, repo_id: str):
        """Clear existing data for a repository."""
        try:
            query = """
            MATCH (n:ASTNode {repo_id: $repo_id})
            DETACH DELETE n
            """
            session.run(query, repo_id=repo_id)
            logger.info(f"Cleared existing data for repo: {repo_id}")
        except Exception as e:
            logger.warning(f"Failed to clear repo data: {e}")

    async def batch_create_nodes(self, nodes: List["ASTNode"], repo_id: str):
        """Create AST nodes in batches."""
        if not self.driver:
            logger.error("Neo4j driver not initialized")
            raise Exception("Neo4j driver not initialized")

        total_batches = (len(nodes) + self.batch_size - 1) // self.batch_size

        for i in range(0, len(nodes), self.batch_size):
            batch_num = (i // self.batch_size) + 1
            batch = nodes[i:i + self.batch_size]

            # Convert nodes to dictionaries
            node_dicts = []
            for node in batch:
                node_dict = {
                    'node_id': node.node_id,
                    'node_type': node.node_type,
                    'start_line': node.start_line,
                    'start_column': node.start_column,
                    'end_line': node.end_line,
                    'end_column': node.end_column,
                    'parent_id': node.parent_id,
                    'file_path': node.file_path,
                    'is_definition': node.is_definition,
                    'is_reference': node.is_reference,
                    'repo_id': repo_id,
                    'name': node.name,
                }
                node_dicts.append(node_dict)

            query = """
            UNWIND $nodes AS node
            CREATE (n:ASTNode)
            SET n = node
            """

            # Add Definition label if is_definition is True
            query += """
            WITH n, node
            WHERE node.is_definition = true
            SET n:Definition
            """

            # Add Reference label if is_reference is True
            query += """
            WITH n, node
            WHERE node.is_reference = true
            SET n:Reference
            """

            def _write_nodes():
                with self.driver.session() as session:
                    session.run(query, nodes=node_dicts)

            try:
                await asyncio.to_thread(_write_nodes)
                logger.debug("Created node batch %s/%s (%s nodes)", batch_num, total_batches, len(batch))
            except Exception as e:
                logger.error("Failed to create node batch %s: %s", batch_num, e)
                raise

    async def batch_create_edges(self, edges: List[Dict[str, Any]], repo_id: str):
        """Create edges in batches."""
        if not self.driver:
            logger.error("Neo4j driver not initialized")
            raise Exception("Neo4j driver not initialized")

        if not edges:
            logger.info("No edges to create")
            return

        total_batches = (len(edges) + self.batch_size - 1) // self.batch_size

        for i in range(0, len(edges), self.batch_size):
            batch_num = (i // self.batch_size) + 1
            batch = edges[i:i + self.batch_size]

            # Group edges by type for efficient processing
            contains_edges = []
            references_edges = []
            depends_on_edges = []

            for edge in batch:
                edge_data = {
                    'source': edge['source'],
                    'target': edge['target'],
                    'sequence': edge.get('sequence', 1),
                }

                if edge['type'] == 'CONTAINS':
                    contains_edges.append(edge_data)
                elif edge['type'] == 'REFERENCES':
                    references_edges.append(edge_data)
                elif edge['type'] == 'DEPENDS_ON':
                    depends_on_edges.append(edge_data)

            def _write_edges():
                with self.driver.session() as session:
                    if contains_edges:
                        contains_query = """
                        UNWIND $edges AS edge
                        MATCH (source:ASTNode {node_id: edge.source})
                        MATCH (target:ASTNode {node_id: edge.target})
                        CREATE (source)-[:CONTAINS {sequence: edge.sequence}]->(target)
                        """
                        session.run(contains_query, edges=contains_edges)

                    if references_edges:
                        references_query = """
                        UNWIND $edges AS edge
                        MATCH (source:ASTNode {node_id: edge.source})
                        MATCH (target:ASTNode {node_id: edge.target})
                        CREATE (source)-[:REFERENCES {sequence: edge.sequence}]->(target)
                        """
                        session.run(references_query, edges=references_edges)

                    if depends_on_edges:
                        depends_on_query = """
                        UNWIND $edges AS edge
                        MATCH (source:ASTNode {node_id: edge.source})
                        MATCH (target:ASTNode {node_id: edge.target})
                        CREATE (source)-[:DEPENDS_ON {sequence: edge.sequence}]->(target)
                        """
                        session.run(depends_on_query, edges=depends_on_edges)

            try:
                await asyncio.to_thread(_write_edges)
                logger.debug(
                    "Created edges in batch %s/%s (contains=%s, references=%s, depends_on=%s)",
                    batch_num,
                    total_batches,
                    len(contains_edges),
                    len(references_edges),
                    len(depends_on_edges),
                )
            except Exception as e:
                logger.error("Failed to create edges in batch %s: %s", batch_num, e)
                raise

    def cross_file_interactions_in_file(self, file_path: str, repo_id: str):
        """Infer cross-file interactions for a given file by finding references to and from definitions in other files."""

        if not self.driver:
            logger.error("Neo4j driver not initialized")
            raise Exception("Neo4j driver not initialized")

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

        with self.driver.session() as session:
            # Downstream
            downstream_result = list(session.run(downstream_query, repo_id=repo_id, file_path=file_path))
            downstream_interactions: Dict[str, List[str]] = {}
            for record in downstream_result:
                def_file = record["def_file_path"]
                interaction = f"{record['ref_name']} REFERENCES {record['node_type']} IN {def_file}"
                downstream_interactions.setdefault(def_file, []).append(interaction)

            downstream_files = {
                record["def_file_path"] for record in downstream_result if record["def_file_path"] != file_path
            }

            # Upstream
            upstream_result = list(session.run(upstream_query, repo_id=repo_id, file_path=file_path))
            upstream_interactions: Dict[str, List[str]] = {}
            for record in upstream_result:
                ref_file = record["ref_file_path"]
                interaction = f"{record['ref_name']} IN {ref_file} REFERENCES {record['node_type']} IN {file_path}"
                upstream_interactions.setdefault(ref_file, []).append(interaction)

            upstream_files = {
                record["ref_file_path"] for record in upstream_result if record["ref_file_path"] != file_path
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

    def close(self):
        """Close the Neo4j connection."""
        if self.driver:
            self.driver.close()
            logger.info("Neo4j connection closed")


class MyMongoClient:
    _instance = None   # Singleton instance

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MyMongoClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, *args, **kwargs):
        # Init runs every time __new__ returns the instance,
        # so guard actual initialization.
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._client = MongoClient(*args, **kwargs)
        self._db = None
        self._initialized = True

    def connect(self, db_name: str):
        """Select the DB to operate on."""
        self._db = self._client[db_name]
    
    def __getitem__(self, collection_name: str):
        return self._db[collection_name]

    # ========== Former helper functions converted to methods ==========

    def get_potential_entry_points(self, repo_id: str) -> List[str]:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        docs = collection.find({
            "repo_id": repo_id,
            "document_type": "POTENTIAL_ENTRY_POINTS"
        })
        return [doc.get("file_path") for doc in docs if doc.get("file_path")]

    def get_repo_summary(self, repo_id: str) -> str:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        doc = collection.find_one({
            "repo_id": repo_id,
            "document_type": "REPO_SUMMARY"
        })
        return doc.get("data", "") if doc else ""

    def get_brief_file_overviews(self, repo_id: str, file_paths: List[str]) -> List[Dict[str, str]]:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        result = []

        for file_path in file_paths:
            doc = collection.find_one(
                {
                    "repo_id": repo_id,
                    "document_type": "BRIEF_FILE_OVERVIEW",
                    "file_path": file_path
                },
                {"_id": 0, "data": 1},
            )
            brief = (doc or {}).get("data", "")
            if brief:
                result.append({"file_path": file_path, "brief": brief})

        return result

    def get_brief_file_overview(self, repo_id: str, file_path: str) -> str:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        doc = collection.find_one(
            {
                "repo_id": repo_id,
                "document_type": "BRIEF_FILE_OVERVIEW",
                "file_path": file_path
            },
            {"_id": 0, "data": 1},
        )
        return (doc or {}).get("data", "")

    def get_critical_file_paths(self, repo_id: str) -> List[str]:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        docs = collection.find({
            "repo_id": repo_id,
            "document_type": "BRIEF_FILE_OVERVIEW"
        })
        return [doc.get("file_path") for doc in docs if doc.get("file_path")]

    def delete_repo_data(self, repo_id: str):
        collections = [CONVERSATIONS_COLLECTION, MESSAGES_COLLECTION, MENTAL_MODEL_COLLECTION, INGESTED_REPOS_COLLECTION]
        for coll_name in collections:
            collection = self._db[coll_name]
            result = collection.delete_many({"repo_id": repo_id})
            logger.info(f"Deleted {result.deleted_count} documents from '{coll_name}' for repo_id '{repo_id}'")
    
    def create_conversation(self, repo_id: str) -> dict:
        collection = self._db[CONVERSATIONS_COLLECTION]
        new_conversation_doc = {
            "repo_id": repo_id,
            "created_at": now_ts(),
            "updated_at": now_ts(),
            "type": "REPO_CHAT",
        }
        result = collection.insert_one(new_conversation_doc)
        return {
            "conversation_id": str(result.inserted_id),
            "repo_id": repo_id,
            "created_at": new_conversation_doc["created_at"],
        }
    
    def list_conversations(
        self,
        *,
        repo_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        query: dict[str, Any] = {}
        if repo_id:
            query["repo_id"] = repo_id

        projection = {"title": 1, "repo_id": 1, "created_at": 1, "updated_at": 1}

        cursor = (
            self._db[CONVERSATIONS_COLLECTION]
            .find(query, projection=projection)
            .sort("updated_at", -1)
            .skip(offset)
            .limit(limit)
        )

        return list(cursor)
    
    def add_ingested_repo(self, repo_name: str, job_id: str):
        collection = self._db[INGESTED_REPOS_COLLECTION]
        existing = collection.find_one({"repo_id": repo_name})
        if existing:
            logger.info(f"Repo '{repo_name}' already marked as ingested.")
            return
        collection.insert_one({"repo_id": repo_name, "job_id": job_id, "ingested_at": datetime.now(timezone.utc).isoformat()})
        logger.info(f"Marked repo '{repo_name}' as ingested.")

    def upsert_ingestion_job(
        self,
        job: IngestionJobStatus,
        *,
        error: dict | None = None,
        extra_fields: dict | None = None,
    ):
        collection = self._db[INGESTION_JOBS_COLLECTION]

        update: dict = {
            "repo_name": job.repo_name,
            "status": job.status,
            "current_stage": job.current_stage.value,
            "updated_at": now_ts(),
        }

        for stage, payload in job.stage_status.items():
            update[f"stages.{stage.value}"] = payload

        if error is not None:
            update["error"] = error

        if extra_fields:
            update.update(extra_fields)

        collection.update_one(
            {"job_id": job.job_id},
            {"$set": update, "$setOnInsert": {"job_id": job.job_id, "created_at": now_ts()}},
            upsert=True,
        )


    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        collection = self._db[INGESTION_JOBS_COLLECTION]
        projection = {"_id": 0}
        job_doc = collection.find_one({"job_id": job_id}, projection)
        job_unfiltered = _serialize_job(job_doc) if job_doc else None
        if job_unfiltered:
            job_filtered = _filter_stage_metrics(job_unfiltered)
            return job_filtered
        return None
    
    def list_jobs(
        self,
        *,
        batch_id: Optional[str] = None,
        status: Optional[str] = None,
        repo_name: Optional[str] = None,
        limit: int = 50,
        skip: int = 0,
        include_total: bool = False,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], int]]:
        q: Dict[str, Any] = {}
        if batch_id:
            q["batch_id"] = batch_id
        if status:
            q["status"] = status
        if repo_name:
            q["repo_name"] = repo_name

        projection = {
            "_id": 0,
            "job_id": 1, "repo_name": 1, "status": 1,
            "current_stage": 1, "error": 1,
            "batch_id": 1, "batch_index": 1,
            "created_at": 1, "updated_at": 1,
        }


        coll = self._db[INGESTION_JOBS_COLLECTION]
        cursor = (
            coll.find(q, projection)
            .sort([("updated_at", -1), ("created_at", -1)])  # stable “most recent first”
            .skip(skip)
            .limit(limit)
        )

        jobs = [d for d in cursor]

        if include_total:
            total = coll.count_documents(q)
            return jobs, total

        return jobs

    def request_abort(self, job_id: str) -> bool:
        res = self._db[INGESTION_JOBS_COLLECTION].update_one(
            {"job_id": job_id},
            {
                "$set": {"abort_requested": True, "abort_requested_at": now_ts(), "updated_at": now_ts()}
            },
        )
        return res.matched_count > 0

    def is_abort_requested(self, job_id: str) -> bool:
        doc = self._db[INGESTION_JOBS_COLLECTION].find_one({"job_id": job_id}, {"_id": 0, "abort_requested": 1})
        return bool(doc and doc.get("abort_requested"))

    def list_ingested_repos(self) -> List[str]:
        collection = self._db[INGESTED_REPOS_COLLECTION]
        docs = collection.find({})
        return [doc.get("repo_id") for doc in docs if doc.get("repo_id")]

    def conversation_exists(self, conversation_id: str) -> bool:
        try:
            oid = ObjectId(conversation_id)
        except Exception:
            raise ValueError("Invalid conversation id")

        doc = self._db[CONVERSATIONS_COLLECTION].find_one({"_id": oid}, {"_id": 1})
        return doc is not None

    def list_conversation_messages(
        self,
        *,
        conversation_id: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))

        cursor = (
            self._db[MESSAGES_COLLECTION]
            .find({"conversation_id": conversation_id}, {"_id": 0})
            .sort("created_at", 1)
            .limit(limit)
        )
        return list(cursor)

    def delete_conversation(self, conversation_id: str) -> None:
        try:
            oid = ObjectId(conversation_id)
        except Exception:
            raise ValueError("Invalid conversation id")

        conv = self._db[CONVERSATIONS_COLLECTION].find_one({"_id": oid}, {"_id": 1})
        if not conv:
            raise KeyError("Conversation not found")

        self._db[MESSAGES_COLLECTION].delete_many({"conversation_id": conversation_id})
        self._db[CONVERSATIONS_COLLECTION].delete_one({"_id": oid})
    
    def get_batch_jobs(self, batch_id: str) -> List[Dict[str, Any]]:
        cursor = (
            self._db[INGESTION_JOBS_COLLECTION]
            .find({"batch_id": batch_id}, {"_id": 0})
            .sort("batch_index", 1)
        )
        return list(cursor)
    
    def get_job(self, job_id: str) -> dict | None:
        return self._db[INGESTION_JOBS_COLLECTION].find_one({"job_id": job_id}, {"_id": 0})

    def delete_job(self, job_id: str) -> bool:
        res = self._db[INGESTION_JOBS_COLLECTION].delete_one({"job_id": job_id})
        return res.deleted_count == 1
    
    def is_repo_ingested(self, repo_name: str) -> bool:
        collection = self._db[INGESTED_REPOS_COLLECTION]
        doc = collection.find_one({"repo_id": repo_name}, {"_id": 1})
        return doc is not None
    
    def is_repo_being_ingested(self, repo_name: str) -> bool:
        collection = self._db[INGESTION_JOBS_COLLECTION]
        doc = collection.find_one(
            {
                "repo_name": repo_name,
                "status": {"$in": ["running", "pending"]}
            },
            {"_id": 1}
        )
        return doc is not None

def init_neo4j_client() -> Neo4jClient:
    """Initialise the global Neo4jClient singleton (if not already)."""
    client = Neo4jClient()
    return client

def get_neo4j_client() -> Neo4jClient:
    """Get the Neo4jClient singleton instance."""
    if Neo4jClient._instance is None:
        init_neo4j_client()
    return Neo4jClient._instance

def init_mongo_client():
    client = MyMongoClient()
    client.connect(Config.MONGO_DB_NAME)
    return client

def get_mongo_client() -> MyMongoClient:
    if MyMongoClient._instance is None:
        init_mongo_client()
    return MyMongoClient._instance
