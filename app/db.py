import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from bson import ObjectId
from pymongo import MongoClient

from .config import Config
from .db_exceptions import (
    ConnectionError as DBConnectionError,
    QueryError,
    InvalidParameterError,
    InvalidConnectionStringError,
)
from .db_retry import with_retry
from .db_metrics import DatabaseMetrics
from .utils import now_ts
from .models.data_model import IngestionJobStatus, IngestionStage

logger = logging.getLogger(__name__)

# Module-level metrics collector (singleton)
_db_metrics = DatabaseMetrics(slow_query_threshold_ms=Config.SLOW_QUERY_THRESHOLD_MS)

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


class LSPCacheReader:
    """Minimal SQLite reader for querying LSP reference cache."""

    def __init__(self, repo_path: str):
        """Initialize reader for a specific repository.

        Args:
            repo_path: Path to repository root (where .lsp_ref_cache.sqlite is located)
        """
        self.repo_path = Path(repo_path)
        self.db_path = self.repo_path / ".lsp_ref_cache.sqlite"
        if not self.db_path.exists():
            raise FileNotFoundError(f"LSP cache not found: {self.db_path}")
        # LSP cache stores paths with repo folder name prefix (e.g., "dictquery/dictquery/__init__.py")
        self.repo_folder_name = self.repo_path.name

    def _get_connection(self) -> sqlite3.Connection:
        """Get a read-only connection to the cache database."""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _to_cache_path(self, file_path: str) -> str:
        """Convert relative file path to LSP cache path format (with repo folder prefix)."""
        return f"{self.repo_folder_name}/{file_path}"

    def _from_cache_path(self, cache_path: str) -> str:
        """Convert LSP cache path to relative file path (strip repo folder prefix)."""
        prefix = f"{self.repo_folder_name}/"
        if cache_path.startswith(prefix):
            return cache_path[len(prefix):]
        return cache_path

    def cross_file_interactions(self, file_path: str) -> Dict[str, Any]:
        """Get cross-file interactions for a given file.

        Args:
            file_path: Relative path to file within repository

        Returns:
            Dict with 'downstream' and 'upstream' keys containing interactions
        """
        conn = self._get_connection()
        # Convert to LSP cache format (with repo folder prefix)
        cache_file_path = self._to_cache_path(file_path)

        try:
            # Downstream: file_path → other files
            downstream_interactions: Dict[str, List[str]] = {}
            downstream_files = set()

            # Note: LSP cache uses language-specific namespaces (e.g., "PythonAnalyzer:pylsp")
            # not repo names, so we don't filter by namespace
            cursor = conn.execute(
                "SELECT ref_path, data FROM mappings WHERE ref_path = ?",
                (cache_file_path,)
            )

            for row in cursor.fetchall():
                data = json.loads(row["data"])
                definitions = data.get("definitions", [])
                if not definitions:
                    continue

                ref = data.get("reference", {})
                ref_loc = f"{file_path}:{ref.get('line', '?')}:{ref.get('column', '?')}"

                for defn in definitions:
                    def_file_cache = defn.get("file_path")
                    if def_file_cache and def_file_cache != cache_file_path:
                        # Convert from cache path to relative path
                        def_file = self._from_cache_path(def_file_cache)
                        def_loc = f"{def_file}:{defn.get('line', '?')}:{defn.get('column', '?')}"
                        interaction = f"{ref_loc} -> {def_loc}"
                        downstream_interactions.setdefault(def_file, []).append(interaction)
                        downstream_files.add(def_file)

            # Upstream: other files → file_path
            upstream_interactions: Dict[str, List[str]] = {}
            upstream_files = set()

            # Query for references that point to definitions in this file
            cursor = conn.execute("""
                SELECT m.ref_path, m.data
                FROM mappings m
                JOIN def_index d ON d.namespace = m.namespace
                    AND d.ref_path = m.ref_path
                    AND d.ref_line = m.ref_line
                    AND d.ref_column = m.ref_column
                WHERE d.def_path = ?
            """, (cache_file_path,))

            for row in cursor.fetchall():
                data = json.loads(row["data"])
                ref = data.get("reference", {})
                ref_file_cache = ref.get("file_path")
                if not ref_file_cache or ref_file_cache == cache_file_path:
                    continue

                # Convert from cache path to relative path
                ref_file = self._from_cache_path(ref_file_cache)
                ref_loc = f"{ref_file}:{ref.get('line', '?')}:{ref.get('column', '?')}"
                definitions = data.get("definitions", [])

                for defn in definitions:
                    def_file_cache = defn.get("file_path")
                    if def_file_cache == cache_file_path:
                        def_loc = f"{file_path}:{defn.get('line', '?')}:{defn.get('column', '?')}"
                        interaction = f"{ref_loc} -> {def_loc}"
                        upstream_interactions.setdefault(ref_file, []).append(interaction)
                        upstream_files.add(ref_file)
                        
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

        finally:
            conn.close()


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
        result = client.create_conversation(repo_name="my-repo")
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

    def get_potential_entry_points(self, repo_name: str) -> List[str]:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        docs = collection.find({
            "repo_name": repo_name,
            "document_type": "POTENTIAL_ENTRY_POINTS"
        })
        return [doc.get("file_path") for doc in docs if doc.get("file_path")]

    def get_repo_summary(self, repo_name: str) -> str:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        doc = collection.find_one({
            "repo_name": repo_name,
            "document_type": "REPO_SUMMARY"
        })
        return doc.get("data", "") if doc else ""

    def get_brief_file_overviews(self, repo_name: str, file_paths: List[str]) -> List[Dict[str, str]]:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        result = []

        for file_path in file_paths:
            doc = collection.find_one(
                {
                    "repo_name": repo_name,
                    "document_type": "BRIEF_FILE_OVERVIEW",
                    "file_path": file_path
                },
                {"_id": 0, "data": 1},
            )
            brief = (doc or {}).get("data", "")
            if brief:
                result.append({"file_path": file_path, "brief": brief})

        return result

    def get_brief_file_overview(self, repo_name: str, file_path: str) -> str:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        doc = collection.find_one(
            {
                "repo_name": repo_name,
                "document_type": "BRIEF_FILE_OVERVIEW",
                "file_path": file_path
            },
            {"_id": 0, "data": 1},
        )
        return (doc or {}).get("data", "")

    def delete_brief_file_overview(self, repo_name: str, file_path: str) -> bool:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        result = collection.delete_one({
            "repo_name": repo_name,
            "document_type": "BRIEF_FILE_OVERVIEW",
            "file_path": file_path
        })
        return result.deleted_count > 0

    def get_critical_file_paths(self, repo_name: str) -> List[str]:
        collection = self._db[MENTAL_MODEL_COLLECTION]
        docs = collection.find({
            "repo_name": repo_name,
            "document_type": "BRIEF_FILE_OVERVIEW"
        })
        return [doc.get("file_path") for doc in docs if doc.get("file_path")]

    @with_retry(max_attempts=3, initial_delay=1.0)
    def delete_repo_data(self, repo_name: str) -> Dict[str, Any]:
        """
        Delete all data for a repository across collections.

        Args:
            repo_name: Repository identifier

        Returns:
            Dict with deletion summary: {
                "repo_name": str,
                "collections_processed": int,
                "total_deleted": int,
                "duration_ms": float,
            }

        Raises:
            InvalidParameterError: If repo_name is invalid
            QueryError: If deletion fails

        Example:
            result = client.delete_repo_data("my-repo")
            logger.info("Deleted %d documents", result["total_deleted"])
        """
        operation = self._metrics.start_operation("delete_repo_data")
        start_time = time.time()

        # VALIDATION: Input checking
        if not repo_name or not isinstance(repo_name, str):
            raise InvalidParameterError("repo_name must be a non-empty string")

        # SECURITY: Sanitize repo_name
        if any(char in repo_name for char in ['$', '{', '}']):
            raise InvalidParameterError("repo_name contains invalid characters")

        try:
            logger.info("Deleting all data for repo_name: %s", repo_name)

            collections = [
                CONVERSATIONS_COLLECTION,
                MESSAGES_COLLECTION,
                MENTAL_MODEL_COLLECTION,
                INGESTED_REPOS_COLLECTION
            ]
            total_deleted = 0

            for coll_name in collections:
                collection = self._db[coll_name]
                result = collection.delete_many({"repo_name": repo_name})
                total_deleted += result.deleted_count
                logger.info(
                    "Deleted documents: collection=%s, repo_name=%s, count=%d",
                    coll_name, repo_name, result.deleted_count
                )

            # Delete ingestion jobs (uses repo_name field)
            ingest_job_coll = self._db[INGESTION_JOBS_COLLECTION]
            result = ingest_job_coll.delete_many({"repo_name": repo_name})
            total_deleted += result.deleted_count
            logger.info(
                "Deleted documents: collection=%s, repo_name=%s, count=%d",
                INGESTION_JOBS_COLLECTION, repo_name, result.deleted_count
            )

            duration_ms = (time.time() - start_time) * 1000
            self._metrics.end_operation(operation, success=True)

            logger.info(
                "Completed repo data deletion: repo_name=%s, total_deleted=%d, duration_ms=%.2f",
                repo_name, total_deleted, duration_ms
            )

            return {
                "repo_name": repo_name,
                "collections_processed": len(collections) + 1,
                "total_deleted": total_deleted,
                "duration_ms": duration_ms,
            }

        except Exception as e:
            self._metrics.end_operation(operation, success=False, error=e)
            logger.error("Failed to delete repo data: repo_name=%s, error=%s", repo_name, str(e))
            raise QueryError(f"Failed to delete repo data: {str(e)}") from e

    @with_retry(max_attempts=3, initial_delay=1.0)
    def create_conversation(self, repo_name: str) -> dict:
        """
        Create a new conversation for a repository.

        Args:
            repo_name: Repository identifier

        Returns:
            Dict with conversation details: {
                "conversation_id": str,
                "repo_name": str,
                "created_at": datetime,
            }

        Raises:
            InvalidParameterError: If repo_name is invalid
            QueryError: If conversation creation fails

        Example:
            result = client.create_conversation(repo_name="my-repo")
            conv_id = result["conversation_id"]
        """
        operation = self._metrics.start_operation("create_conversation")
        start_time = time.time()

        # VALIDATION: Input checking
        if not repo_name or not isinstance(repo_name, str):
            raise InvalidParameterError("repo_name must be a non-empty string")

        # SECURITY: Sanitize repo_name (basic check)
        if any(char in repo_name for char in ['$', '{', '}']):
            raise InvalidParameterError("repo_name contains invalid characters")

        try:
            collection = self._db[CONVERSATIONS_COLLECTION]

            new_conversation_doc = {
                "repo_name": repo_name,
                "created_at": now_ts(),
                "updated_at": now_ts(),
                "type": "REPO_CHAT",
            }

            result = collection.insert_one(new_conversation_doc)

            duration_ms = (time.time() - start_time) * 1000
            self._metrics.end_operation(operation, success=True)

            logger.info(
                "Created conversation: conversation_id=%s, repo_name=%s, duration_ms=%.2f",
                str(result.inserted_id), repo_name, duration_ms
            )

            return {
                "conversation_id": str(result.inserted_id),
                "repo_name": repo_name,
                "created_at": new_conversation_doc["created_at"],
            }

        except Exception as e:
            self._metrics.end_operation(operation, success=False, error=e)
            logger.error("Failed to create conversation: repo_name=%s, error=%s", repo_name, str(e))
            raise QueryError(f"Failed to create conversation: {str(e)}") from e
    
    def list_conversations(
        self,
        *,
        repo_name: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        query: dict[str, Any] = {}
        if repo_name:
            query["repo_name"] = repo_name

        projection = {"title": 1, "repo_name": 1, "created_at": 1, "updated_at": 1}

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
        existing = collection.find_one({"repo_name": repo_name})
        if existing:
            logger.info(f"Repo '{repo_name}' already marked as ingested.")
            return
        collection.insert_one({"repo_name": repo_name, "job_id": job_id, "ingested_at": datetime.now(timezone.utc).isoformat()})
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
        return [doc.get("repo_name") for doc in docs if doc.get("repo_name")]

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
        doc = collection.find_one({"repo_name": repo_name}, {"_id": 1})
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


def init_mongo_client():
    client = MyMongoClient()
    client.connect(Config.MONGO_DB_NAME)
    return client

def get_mongo_client() -> MyMongoClient:
    if MyMongoClient._instance is None:
        init_mongo_client()
    return MyMongoClient._instance
