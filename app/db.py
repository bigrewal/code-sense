import logging
from neo4j import GraphDatabase
from pymongo import MongoClient
from .config import Config
from .models.data_model import CodeGraph, ASTNode

from typing import Dict, Tuple
from typing import List, Dict, Any
import time
logger = logging.getLogger(__name__)

## Silence WARNING:neo4j.notifications: 
logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)


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

        try:
            logger.info(f"Initialising graph for repo: {repo_id}")

            with self.driver.session() as session:
                # Create indexes if they don't exist
                self._create_indexes(session)

                # Optional: Clear existing data for this repo
                self.clear_repo_data(session, repo_id)

        except Exception as exc:
            logger.error(f"Neo4j initialisation failed: {exc}")
            raise

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

        with self.driver.session() as session:
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

                try:
                    session.run(query, nodes=node_dicts)
                    logger.debug(f"Created node batch {batch_num}/{total_batches} ({len(batch)} nodes)")
                except Exception as e:
                    logger.error(f"Failed to create node batch {batch_num}: {e}")
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

        with self.driver.session() as session:
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

                # Create CONTAINS relationships
                if contains_edges:
                    contains_query = """
                    UNWIND $edges AS edge
                    MATCH (source:ASTNode {node_id: edge.source})
                    MATCH (target:ASTNode {node_id: edge.target})
                    CREATE (source)-[:CONTAINS {sequence: edge.sequence}]->(target)
                    """
                    try:
                        session.run(contains_query, edges=contains_edges)
                        logger.debug(f"Created {len(contains_edges)} CONTAINS edges in batch {batch_num}")
                    except Exception as e:
                        logger.error(f"Failed to create CONTAINS edges in batch {batch_num}: {e}")
                        raise

                # Create REFERENCES relationships
                if references_edges:
                    references_query = """
                    UNWIND $edges AS edge
                    MATCH (source:ASTNode {node_id: edge.source})
                    MATCH (target:ASTNode {node_id: edge.target})
                    CREATE (source)-[:REFERENCES {sequence: edge.sequence}]->(target)
                    """
                    try:
                        session.run(references_query, edges=references_edges)
                        logger.debug(f"Created {len(references_edges)} REFERENCES edges in batch {batch_num}")
                    except Exception as e:
                        logger.error(f"Failed to create REFERENCES edges in batch {batch_num}: {e}")
                        raise

                # Create DEPENDS_ON relationships
                if depends_on_edges:
                    depends_on_query = """
                    UNWIND $edges AS edge
                    MATCH (source:ASTNode {node_id: edge.source})
                    MATCH (target:ASTNode {node_id: edge.target})
                    CREATE (source)-[:DEPENDS_ON {sequence: edge.sequence}]->(target)
                    """
                    try:
                        session.run(depends_on_query, edges=depends_on_edges)
                        logger.debug(f"Created {len(depends_on_edges)} DEPENDS_ON edges in batch {batch_num}")
                    except Exception as e:
                        logger.error(f"Failed to create DEPENDS_ON edges in batch {batch_num}: {e}")
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
        collection = self._db["mental_model"]
        docs = collection.find({
            "repo_id": repo_id,
            "document_type": "POTENTIAL_ENTRY_POINTS"
        })
        return [doc.get("file_path") for doc in docs if doc.get("file_path")]

    def get_repo_summary(self, repo_id: str) -> str:
        collection = self._db["mental_model"]
        doc = collection.find_one({
            "repo_id": repo_id,
            "document_type": "REPO_SUMMARY"
        })
        return doc.get("data", "") if doc else ""

    def get_brief_file_overviews(self, repo_id: str, file_paths: List[str]) -> List[Dict[str, str]]:
        collection = self._db["mental_model"]
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
        collection = self._db["mental_model"]
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
        collection = self._db["mental_model"]
        docs = collection.find({
            "repo_id": repo_id,
            "document_type": "BRIEF_FILE_OVERVIEW"
        })
        return [doc.get("file_path") for doc in docs if doc.get("file_path")]

    def delete_repo_data(self, repo_id: str):
        collections = ["conversations", "messages", "mental_model", "ingested_repos"]
        for coll_name in collections:
            collection = self._db[coll_name]
            result = collection.delete_many({"repo_id": repo_id})
            logger.info(f"Deleted {result.deleted_count} documents from '{coll_name}' for repo_id '{repo_id}'")

    
    def add_ingested_repo(self, repo_name: str, job_id: str):
        collection = self._db["ingested_repos"]
        existing = collection.find_one({"repo_id": repo_name})
        if existing:
            logger.info(f"Repo '{repo_name}' already marked as ingested.")
            return
        collection.insert_one({"repo_id": repo_name, "job_id": job_id, "ingested_at": time.time()})
        logger.info(f"Marked repo '{repo_name}' as ingested.")

    def upsert_ingestion_job(
        self,
        job_id: str,
        repo_name: str,
        *,
        status: str,
        current_stage: str | None = None,
        stage_status: dict | None = None,
        error: dict | None = None,
    ):
        """
        Create or update an ingestion job and its stage status.
        """
        collection = self._db["ingestion_jobs"]

        update: dict = {
            "repo_name": repo_name,
            "status": status,
            "updated_at": time.time(),
        }

        if current_stage:
            update["current_stage"] = current_stage

        if stage_status:
            # example: { "precheck": { "status": "completed", "metrics": {...} } }
            for stage, payload in stage_status.items():
                update[f"stages.{stage}"] = payload

        if error:
            update["error"] = error

        collection.update_one(
            {"job_id": job_id},
            {
                "$set": update,
                "$setOnInsert": {
                    "job_id": job_id,
                    "created_at": time.time(),
                },
            },
            upsert=True,
        )

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        collection = self._db["ingestion_jobs"]
        job_doc = collection.find_one({"job_id": job_id})
        if not job_doc:
            raise ValueError(f"No ingestion job found for job_id: {job_id}")
        
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


    def list_ingested_repos(self) -> List[str]:
        collection = self._db["ingested_repos"]
        docs = collection.find({})
        return [doc.get("repo_id") for doc in docs if doc.get("repo_id")]

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
