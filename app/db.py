import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from bson import ObjectId
from neo4j import GraphDatabase
from pymongo import MongoClient

from .config import Config
from .db_exceptions import (
    ConnectionError as DBConnectionError,
    ConnectionTimeoutError,
    QueryError,
    ValidationError,
    InvalidParameterError,
    AuthenticationError,
    InvalidConnectionStringError,
    Neo4jHealthCheckError,
    MongoHealthCheckError,
)
from .db_retry import with_retry, with_retry_async
from .db_metrics import DatabaseMetrics
from .utils import now_ts
from .models.data_model import ASTNode, IngestionJobStatus, IngestionStage

logger = logging.getLogger(__name__)

# Module-level metrics collector (singleton)
_db_metrics = DatabaseMetrics(slow_query_threshold_ms=Config.SLOW_QUERY_THRESHOLD_MS)

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
        "supported_file_count",
        "unsupported_file_count",
        "language_distribution_pct",
        "supported_tokens"
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
    """
    Neo4j database client wrapper with connection pooling, retry logic, and health monitoring.

    Features:
    - Singleton pattern for application-wide connection reuse
    - Automatic retry on transient failures
    - Connection pool monitoring
    - Query performance tracking
    - Comprehensive error handling

    Example:
        client = get_neo4j_client()
        await client.batch_create_nodes(nodes, repo_id="my-repo")
    """

    _instance = None  # Singleton instance
    _lock = threading.Lock()  # Thread-safe singleton initialization

    def __new__(cls, *args, **kwargs):
        """Enforce thread-safe singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super(Neo4jClient, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize Neo4j client with connection pooling and retry logic."""
        # Prevent re-initialization on subsequent instantiations
        if getattr(self, "_initialized", False):
            return

        self.driver = None
        self.batch_size = Config.NEO4J_BATCH_SIZE
        self._metrics = _db_metrics

        # SECURITY: Don't log credentials
        logger.info("Initializing Neo4jClient connection")

        try:
            # VALIDATION: Check connection configuration
            self._validate_connection_config()

            # PERFORMANCE: Configure connection pool
            self.driver = GraphDatabase.driver(
                Config.NEO4J_URI,
                auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
                max_connection_lifetime=Config.NEO4J_MAX_CONNECTION_LIFETIME,
                max_connection_pool_size=Config.NEO4J_MAX_POOL_SIZE,
                connection_timeout=Config.NEO4J_CONNECTION_TIMEOUT,
                keep_alive=True,
            )

            # Test connection with retry
            self._initialize_database_with_retry()

            logger.info("Neo4j connection established successfully")

        except DBConnectionError as e:
            logger.error("Neo4j connection failed: %s", str(e))
            raise
        except Exception as e:
            logger.exception("Unexpected error during Neo4j initialization")
            raise DBConnectionError(f"Failed to initialize Neo4j client: {str(e)}") from e

        self._initialized = True

    def _validate_connection_config(self):
        """
        Validate Neo4j connection configuration.

        Raises:
            InvalidConnectionStringError: If connection URI is invalid
            AuthenticationError: If credentials are not configured
        """
        if not Config.NEO4J_URI:
            raise InvalidConnectionStringError("NEO4J_URI not configured")

        valid_schemes = ("bolt://", "neo4j://", "bolt+s://", "neo4j+s://")
        if not Config.NEO4J_URI.startswith(valid_schemes):
            raise InvalidConnectionStringError(
                f"Invalid Neo4j URI scheme. Must start with one of: {valid_schemes}"
            )

        if not Config.NEO4J_USER or not Config.NEO4J_PASSWORD:
            raise AuthenticationError("Neo4j credentials not configured")

    @with_retry(max_attempts=3, initial_delay=2.0)
    def _initialize_database_with_retry(self):
        """
        Initialize database with index creation (with retry for transient failures).

        Raises:
            DBConnectionError: If initialization fails after retries
        """
        try:
            with self.driver.session() as session:
                self._create_indexes(session)
                session.run("CALL db.awaitIndexes()")
                session.run("RETURN 1")
        except Exception as e:
            raise DBConnectionError(f"Failed to initialize database: {str(e)}") from e

    async def init_graph_for_repo(self, repo_id: str):
        if not self.driver:
            logger.error("Neo4j driver not initialized")
            raise Exception("Neo4j driver not initialized")

        def _initialise():
            try:
                logger.info("Initialising graph for repo: %s", repo_id)
                with self.driver.session() as session:
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
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.repo_id, n.node_id)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.file_path)",
            "CREATE INDEX IF NOT EXISTS FOR (n:ASTNode) ON (n.node_type)",
        ]

        for index_query in indexes:
            session.run(index_query).consume()

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

    def _serialize_nodes(self, nodes: List[ASTNode], repo_id: str) -> List[Dict[str, Any]]:
        """
        Serialize AST nodes to dictionaries with validation.

        Args:
            nodes: List of ASTNode objects to serialize
            repo_id: Repository identifier for validation

        Returns:
            List of node dictionaries ready for Neo4j insertion

        Raises:
            ValidationError: If node validation fails
        """
        node_dicts = []

        for idx, node in enumerate(nodes):
            # VALIDATION: Type checking
            if not isinstance(node, ASTNode):
                raise ValidationError(
                    f"Node at index {idx} is not an ASTNode: {type(node).__name__}"
                )

            # VALIDATION: Required fields
            if not node.node_id:
                raise ValidationError(f"Node at index {idx} missing node_id")
            if not node.file_path:
                raise ValidationError(f"Node at index {idx} missing file_path")

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

        return node_dicts

    @with_retry_async(max_attempts=3, initial_delay=1.0)
    async def batch_create_nodes(
        self,
        nodes: List[ASTNode],
        repo_id: str,
        batch_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create AST nodes in batches with retry and monitoring.

        Args:
            nodes: List of AST nodes to create
            repo_id: Repository identifier
            batch_size: Optional batch size override (default: Config.NEO4J_BATCH_SIZE)

        Returns:
            Dict with operation summary: {
                "total_nodes": int,
                "batches_processed": int,
                "duration_ms": float,
                "errors": List[str],
            }

        Raises:
            InvalidParameterError: If inputs are invalid
            QueryError: If batch creation fails after retries
            DBConnectionError: If driver not initialized

        Example:
            result = await client.batch_create_nodes(nodes, "my-repo")
            logger.info("Created %d nodes in %d batches",
                        result["total_nodes"], result["batches_processed"])
        """
        start_time = time.time()
        operation = self._metrics.start_operation("batch_create_nodes")

        # VALIDATION: Input checking
        if not repo_id or not isinstance(repo_id, str):
            raise InvalidParameterError("repo_id must be a non-empty string")

        if not nodes:
            logger.warning("batch_create_nodes called with empty node list for repo: %s", repo_id)
            return {"total_nodes": 0, "batches_processed": 0, "duration_ms": 0.0, "errors": []}

        if not isinstance(nodes, list):
            raise InvalidParameterError("nodes must be a list of ASTNode objects")

        # Driver check
        if not self.driver:
            raise DBConnectionError("Neo4j driver not initialized")

        batch_size = batch_size or self.batch_size
        total_batches = (len(nodes) + batch_size - 1) // batch_size
        errors = []
        batches_processed = 0

        logger.info(
            "Starting batch node creation: repo_id=%s, total_nodes=%d, batch_size=%d, batches=%d",
            repo_id, len(nodes), batch_size, total_batches
        )

        try:
            for i in range(0, len(nodes), batch_size):
                batch_num = (i // batch_size) + 1
                batch = nodes[i:i + batch_size]

                batch_start = time.time()

                # Convert nodes to dictionaries with validation
                node_dicts = self._serialize_nodes(batch, repo_id)

                query = """
                UNWIND $nodes AS node
                CREATE (n:ASTNode)
                SET n = node
                WITH n, node
                WHERE node.is_definition = true
                SET n:Definition
                WITH n, node
                WHERE node.is_reference = true
                SET n:Reference
                """

                def _write_nodes():
                    with self.driver.session() as session:
                        session.run(query, nodes=node_dicts)

                try:
                    await asyncio.to_thread(_write_nodes)
                    batches_processed += 1

                    batch_duration_ms = (time.time() - batch_start) * 1000

                    # MONITORING: Track slow batches
                    if batch_duration_ms > Config.SLOW_QUERY_THRESHOLD_MS:
                        logger.warning(
                            "Slow batch operation: repo_id=%s, batch=%d/%d, nodes=%d, duration_ms=%.2f",
                            repo_id, batch_num, total_batches, len(batch), batch_duration_ms
                        )

                    logger.debug(
                        "Created node batch: repo_id=%s, batch=%d/%d, nodes=%d, duration_ms=%.2f",
                        repo_id, batch_num, total_batches, len(batch), batch_duration_ms
                    )

                except Exception as e:
                    error_msg = f"Batch {batch_num}/{total_batches} failed: {str(e)}"
                    errors.append(error_msg)
                    logger.error(
                        "Failed to create node batch: repo_id=%s, batch=%d/%d, error=%s",
                        repo_id, batch_num, total_batches, str(e)
                    )
                    raise QueryError(error_msg) from e

            duration_ms = (time.time() - start_time) * 1000
            self._metrics.end_operation(operation, success=True)

            logger.info(
                "Completed batch node creation: repo_id=%s, total_nodes=%d, batches=%d, duration_ms=%.2f",
                repo_id, len(nodes), batches_processed, duration_ms
            )

            return {
                "total_nodes": len(nodes),
                "batches_processed": batches_processed,
                "duration_ms": duration_ms,
                "errors": errors,
            }

        except Exception as e:
            self._metrics.end_operation(operation, success=False, error=e)
            raise

    @with_retry_async(max_attempts=3, initial_delay=1.0)
    async def batch_create_edges(
        self,
        edges: List[Dict[str, Any]],
        repo_id: str,
        batch_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create edges in batches with retry and monitoring.

        Args:
            edges: List of edge dictionaries with 'source', 'target', 'type', 'sequence'
            repo_id: Repository identifier
            batch_size: Optional batch size override (default: Config.NEO4J_BATCH_SIZE)

        Returns:
            Dict with operation summary: {
                "total_edges": int,
                "batches_processed": int,
                "duration_ms": float,
                "errors": List[str],
            }

        Raises:
            InvalidParameterError: If inputs are invalid
            QueryError: If batch creation fails after retries
            DBConnectionError: If driver not initialized

        Example:
            result = await client.batch_create_edges(edges, "my-repo")
            logger.info("Created %d edges in %d batches",
                        result["total_edges"], result["batches_processed"])
        """
        start_time = time.time()
        operation = self._metrics.start_operation("batch_create_edges")

        # VALIDATION: Input checking
        if not repo_id or not isinstance(repo_id, str):
            raise InvalidParameterError("repo_id must be a non-empty string")

        if not edges:
            logger.info("No edges to create for repo: %s", repo_id)
            return {"total_edges": 0, "batches_processed": 0, "duration_ms": 0.0, "errors": []}

        if not isinstance(edges, list):
            raise InvalidParameterError("edges must be a list of dictionaries")

        # Driver check
        if not self.driver:
            raise DBConnectionError("Neo4j driver not initialized")

        batch_size = batch_size or self.batch_size
        total_batches = (len(edges) + batch_size - 1) // batch_size
        errors = []
        batches_processed = 0

        try:
            for i in range(0, len(edges), batch_size):
                batch_num = (i // batch_size) + 1
                batch = edges[i:i + batch_size]

                batch_start = time.time()

                # Validate and prepare edges
                edges_payload = []
                for idx, e in enumerate(batch):
                    if not isinstance(e, dict):
                        raise ValidationError(f"Edge at index {idx} is not a dictionary")
                    if "source" not in e or "target" not in e or "type" not in e:
                        raise ValidationError(f"Edge at index {idx} missing required fields")

                    edges_payload.append({
                        "source": e["source"],
                        "target": e["target"],
                        "sequence": e.get("sequence", 1),
                        "type": e["type"],
                    })

                query = """
                UNWIND $edges AS edge
                MATCH (s:ASTNode {repo_id: $repo_id, node_id: edge.source})
                MATCH (t:ASTNode {repo_id: $repo_id, node_id: edge.target})
                CALL {
                    WITH s, t, edge
                    WITH s, t, edge WHERE edge.type = 'CONTAINS'
                    CREATE (s)-[:CONTAINS {sequence: edge.sequence}]->(t)
                }
                CALL {
                    WITH s, t, edge
                    WITH s, t, edge WHERE edge.type = 'REFERENCES'
                    CREATE (s)-[:REFERENCES {sequence: edge.sequence}]->(t)
                }
                """

                def _write_edges():
                    with self.driver.session() as session:
                        session.execute_write(
                            lambda tx: tx.run(query, edges=edges_payload, repo_id=repo_id)
                        )

                try:
                    await asyncio.to_thread(_write_edges)
                    batches_processed += 1

                    batch_duration_ms = (time.time() - batch_start) * 1000

                    # MONITORING: Track slow batches
                    if batch_duration_ms > Config.SLOW_QUERY_THRESHOLD_MS:
                        logger.warning(
                            "Slow batch operation: repo_id=%s, batch=%d/%d, edges=%d, duration_ms=%.2f",
                            repo_id, batch_num, total_batches, len(edges_payload), batch_duration_ms
                        )

                    logger.debug(
                        "Created edge batch: repo_id=%s, batch=%d/%d, edges=%d, duration_ms=%.2f",
                        repo_id, batch_num, total_batches, len(edges_payload), batch_duration_ms
                    )

                except Exception as e:
                    error_msg = f"Batch {batch_num}/{total_batches} failed: {str(e)}"
                    errors.append(error_msg)
                    logger.error(
                        "Failed to create edge batch: repo_id=%s, batch=%d/%d, error=%s",
                        repo_id, batch_num, total_batches, str(e)
                    )
                    raise QueryError(error_msg) from e

            duration_ms = (time.time() - start_time) * 1000
            self._metrics.end_operation(operation, success=True)

            return {
                "total_edges": len(edges),
                "batches_processed": batches_processed,
                "duration_ms": duration_ms,
                "errors": errors,
            }

        except Exception as e:
            self._metrics.end_operation(operation, success=False, error=e)
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

    def health_check(self) -> Dict[str, Any]:
        """
        Perform health check on Neo4j connection.

        Returns:
            Dict with health status: {
                "status": "healthy" | "unhealthy",
                "response_time_ms": float,
                "connection_pool": {"in_use": int, "idle": int},
                "error": Optional[str],
            }

        Example:
            health = client.health_check()
            if health["status"] == "unhealthy":
                logger.error("Neo4j is down: %s", health.get("error"))
        """
        start_time = time.time()
        
        if not self.driver:
            return {
                "status": "unhealthy",
                "error": "Driver not initialized",
                "response_time_ms": 0.0,
            }

        try:
            # Simple query to test connectivity
            with self.driver.session() as session:
                result = session.run("RETURN 1 AS health_check")
                result.single()

            response_time_ms = (time.time() - start_time) * 1000

            # Get connection pool metrics (placeholder for future enhancement)
            pool_stats = {"in_use": 0, "idle": 0}

            health_status = {
                "status": "healthy",
                "response_time_ms": response_time_ms,
                "connection_pool": pool_stats,
            }

            return health_status

        except Exception as e:
            logger.error("Neo4j health check failed: %s", str(e))
            return {
                "status": "unhealthy",
                "error": str(e),
                "response_time_ms": (time.time() - start_time) * 1000,
            }

    def close(self):
        """
        Close the Neo4j connection gracefully.

        Ensures all in-flight operations complete before closing.
        Should be called during application shutdown.
        """
        if not self.driver:
            logger.warning("close() called but driver was not initialized")
            return

        try:
            logger.info("Closing Neo4j connection")

            # Give in-flight operations time to complete
            time.sleep(0.5)

            self.driver.close()
            logger.info("Neo4j connection closed successfully")

        except Exception as e:
            logger.error("Error closing Neo4j connection: %s", str(e))
            raise
        finally:
            self.driver = None


class MyMongoClient:
    """
    MongoDB database client wrapper with connection pooling, retry logic, and health monitoring.

    Features:
    - Singleton pattern for application-wide connection reuse
    - Automatic retry on transient failures
    - Connection pool configuration
    - Query performance tracking
    - Input validation and sanitization

    Example:
        client = get_mongo_client()
        result = client.create_conversation(repo_id="my-repo")
    """

    _instance = None   # Singleton instance
    _lock = threading.Lock()  # Thread-safe singleton initialization

    def __new__(cls, *args, **kwargs):
        """Enforce thread-safe singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super(MyMongoClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, *args, **kwargs):
        """Initialize MongoDB client with connection pooling and retry logic."""
        # Init runs every time __new__ returns the instance,
        # so guard actual initialization.
        if hasattr(self, "_initialized") and self._initialized:
            return

        logger.info("Initializing MyMongoClient connection")

        try:
            # VALIDATION: Check connection string
            if not Config.MONGO_URI:
                raise InvalidConnectionStringError("MONGO_URI not configured")

            # PERFORMANCE: Configure connection pool
            self._client = MongoClient(
                Config.MONGO_URI,
                maxPoolSize=Config.MONGO_MAX_POOL_SIZE,
                minPoolSize=Config.MONGO_MIN_POOL_SIZE,
                maxIdleTimeMS=Config.MONGO_MAX_IDLE_TIME_MS,
                serverSelectionTimeoutMS=Config.MONGO_SERVER_SELECTION_TIMEOUT_MS,
                connectTimeoutMS=Config.MONGO_CONNECT_TIMEOUT_MS,
                socketTimeoutMS=Config.MONGO_SOCKET_TIMEOUT_MS,
                retryWrites=True,
                retryReads=True,
                *args,
                **kwargs
            )

            self._db = None
            self._metrics = _db_metrics
            self._initialized = True

            logger.info("MongoDB client initialized successfully")

        except Exception as e:
            logger.exception("Failed to initialize MongoDB client")
            raise DBConnectionError(f"Failed to initialize MongoDB client: {str(e)}") from e

    def connect(self, db_name: str):
        """
        Select the database to operate on with validation.

        Args:
            db_name: Database name

        Raises:
            InvalidParameterError: If db_name is invalid
            DBConnectionError: If connection fails
        """
        if not db_name or not isinstance(db_name, str):
            raise InvalidParameterError("db_name must be a non-empty string")

        try:
            self._db = self._client[db_name]
            # Test connection
            self._db.command("ping")
            logger.info("Connected to MongoDB database: %s", db_name)
        except Exception as e:
            logger.error("Failed to connect to database %s: %s", db_name, str(e))
            raise DBConnectionError(f"Failed to connect to database: {str(e)}") from e
    
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

    @with_retry(max_attempts=3, initial_delay=1.0)
    def delete_repo_data(self, repo_id: str) -> Dict[str, Any]:
        """
        Delete all data for a repository across collections.

        Args:
            repo_id: Repository identifier

        Returns:
            Dict with deletion summary: {
                "repo_id": str,
                "collections_processed": int,
                "total_deleted": int,
                "duration_ms": float,
            }

        Raises:
            InvalidParameterError: If repo_id is invalid
            QueryError: If deletion fails

        Example:
            result = client.delete_repo_data("my-repo")
            logger.info("Deleted %d documents", result["total_deleted"])
        """
        operation = self._metrics.start_operation("delete_repo_data")
        start_time = time.time()

        # VALIDATION: Input checking
        if not repo_id or not isinstance(repo_id, str):
            raise InvalidParameterError("repo_id must be a non-empty string")

        # SECURITY: Sanitize repo_id
        if any(char in repo_id for char in ['$', '{', '}']):
            raise InvalidParameterError("repo_id contains invalid characters")

        try:
            logger.info("Deleting all data for repo_id: %s", repo_id)

            collections = [
                CONVERSATIONS_COLLECTION,
                MESSAGES_COLLECTION,
                MENTAL_MODEL_COLLECTION,
                INGESTED_REPOS_COLLECTION
            ]
            total_deleted = 0

            for coll_name in collections:
                collection = self._db[coll_name]
                result = collection.delete_many({"repo_id": repo_id})
                total_deleted += result.deleted_count
                logger.info(
                    "Deleted documents: collection=%s, repo_id=%s, count=%d",
                    coll_name, repo_id, result.deleted_count
                )

            # Delete ingestion jobs (uses repo_name field)
            ingest_job_coll = self._db[INGESTION_JOBS_COLLECTION]
            result = ingest_job_coll.delete_many({"repo_name": repo_id})
            total_deleted += result.deleted_count
            logger.info(
                "Deleted documents: collection=%s, repo_id=%s, count=%d",
                INGESTION_JOBS_COLLECTION, repo_id, result.deleted_count
            )

            duration_ms = (time.time() - start_time) * 1000
            self._metrics.end_operation(operation, success=True)

            logger.info(
                "Completed repo data deletion: repo_id=%s, total_deleted=%d, duration_ms=%.2f",
                repo_id, total_deleted, duration_ms
            )

            return {
                "repo_id": repo_id,
                "collections_processed": len(collections) + 1,
                "total_deleted": total_deleted,
                "duration_ms": duration_ms,
            }

        except Exception as e:
            self._metrics.end_operation(operation, success=False, error=e)
            logger.error("Failed to delete repo data: repo_id=%s, error=%s", repo_id, str(e))
            raise QueryError(f"Failed to delete repo data: {str(e)}") from e

    @with_retry(max_attempts=3, initial_delay=1.0)
    def create_conversation(self, repo_id: str) -> dict:
        """
        Create a new conversation for a repository.

        Args:
            repo_id: Repository identifier

        Returns:
            Dict with conversation details: {
                "conversation_id": str,
                "repo_id": str,
                "created_at": datetime,
            }

        Raises:
            InvalidParameterError: If repo_id is invalid
            QueryError: If conversation creation fails

        Example:
            result = client.create_conversation(repo_id="my-repo")
            conv_id = result["conversation_id"]
        """
        operation = self._metrics.start_operation("create_conversation")
        start_time = time.time()

        # VALIDATION: Input checking
        if not repo_id or not isinstance(repo_id, str):
            raise InvalidParameterError("repo_id must be a non-empty string")

        # SECURITY: Sanitize repo_id (basic check)
        if any(char in repo_id for char in ['$', '{', '}']):
            raise InvalidParameterError("repo_id contains invalid characters")

        try:
            collection = self._db[CONVERSATIONS_COLLECTION]

            new_conversation_doc = {
                "repo_id": repo_id,
                "created_at": now_ts(),
                "updated_at": now_ts(),
                "type": "REPO_CHAT",
            }

            result = collection.insert_one(new_conversation_doc)

            duration_ms = (time.time() - start_time) * 1000
            self._metrics.end_operation(operation, success=True)

            logger.info(
                "Created conversation: conversation_id=%s, repo_id=%s, duration_ms=%.2f",
                str(result.inserted_id), repo_id, duration_ms
            )

            return {
                "conversation_id": str(result.inserted_id),
                "repo_id": repo_id,
                "created_at": new_conversation_doc["created_at"],
            }

        except Exception as e:
            self._metrics.end_operation(operation, success=False, error=e)
            logger.error("Failed to create conversation: repo_id=%s, error=%s", repo_id, str(e))
            raise QueryError(f"Failed to create conversation: {str(e)}") from e
    
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

    def health_check(self) -> Dict[str, Any]:
        """
        Perform health check on MongoDB connection.

        Returns:
            Dict with health status: {
                "status": "healthy" | "unhealthy",
                "response_time_ms": float,
                "database": str,
                "collections": List[str],
                "error": Optional[str],
            }

        Example:
            health = client.health_check()
            if health["status"] == "unhealthy":
                logger.error("MongoDB is down: %s", health.get("error"))
        """
        start_time = time.time()

        # Fix: MongoDB database objects don't support truth value testing
        # Must compare with None explicitly
        if self._client is None or self._db is None:
            return {
                "status": "unhealthy",
                "error": "Client not initialized or database not selected",
                "response_time_ms": 0.0,
            }

        try:
            # Ping database
            self._db.command("ping")

            # Get collection names
            collections = self._db.list_collection_names()

            response_time_ms = (time.time() - start_time) * 1000

            return {
                "status": "healthy",
                "response_time_ms": response_time_ms,
                "database": self._db.name,
                "collections": collections,
                "collection_count": len(collections),
            }

        except Exception as e:
            logger.error("MongoDB health check failed: %s", str(e))
            return {
                "status": "unhealthy",
                "error": str(e),
                "response_time_ms": (time.time() - start_time) * 1000,
            }

    def close(self):
        """
        Close the MongoDB connection gracefully.

        Ensures all in-flight operations complete before closing.
        Should be called during application shutdown.
        """
        if not self._client:
            logger.warning("close() called but client was not initialized")
            return

        try:
            logger.info("Closing MongoDB connection")

            # Give in-flight operations time to complete
            time.sleep(0.5)

            self._client.close()
            logger.info("MongoDB connection closed successfully")

        except Exception as e:
            logger.error("Error closing MongoDB connection: %s", str(e))
            raise
        finally:
            self._client = None
            self._db = None


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
