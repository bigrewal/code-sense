import logging
import threading
import time
from datetime import datetime, timezone
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
from .utils import now_ts
from .models.data_model import IngestionJobStatus, IngestionStage

logger = logging.getLogger(__name__)

# Module-level metrics collector (singleton)

INGESTION_JOBS_COLLECTION = "ingestion_jobs"
INGESTED_REPOS_COLLECTION = "ingested_repos"
CONVERSATIONS_COLLECTION = "conversations"
MESSAGES_COLLECTION = "messages"
MENTAL_MODEL_COLLECTION = "mental_model"
INGESTION_FILE_STATE_COLLECTION = "ingestion_file_state"
def _serialize_job(job_doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job_doc.get("job_id"),
        "repo_name": job_doc.get("repo_name"),
        "status": job_doc.get("status"),
        "current_stage": job_doc.get("current_stage"),
        "stages": job_doc.get("stages", {}),
        "error": job_doc.get("error"),
        "operation": job_doc.get("operation"),
        "created_at": job_doc.get("created_at"),
        "updated_at": job_doc.get("updated_at"),
    }

def _filter_stage_metrics(job: Dict[str, Any]) -> Dict[str, Any]:
    ALLOWED_PRECHECK_METRICS = {
        "skipped",
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


def _validate_repo_name(repo_name: str) -> None:
    if not repo_name or not isinstance(repo_name, str):
        raise InvalidParameterError("repo_name must be a non-empty string")
    if any(char in repo_name for char in ["$", "{", "}"]):
        raise InvalidParameterError("repo_name contains invalid characters")


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

    def get_repo_file_states(self, repo_name: str) -> Dict[str, Dict[str, Any]]:
        docs = self._db[INGESTION_FILE_STATE_COLLECTION].find(
            {"repo_name": repo_name},
            {"_id": 0, "file_path": 1, "sha1": 1, "language": 1, "supported": 1, "token_count": 1},
        )
        return {d["file_path"]: d for d in docs if d.get("file_path")}

    def upsert_repo_file_states(self, repo_name: str, rows: List[Dict[str, Any]]) -> None:
        collection = self._db[INGESTION_FILE_STATE_COLLECTION]
        now = now_ts()
        for row in rows:
            file_path = row["file_path"]
            update = {
                "repo_name": repo_name,
                "file_path": file_path,
                "sha1": row["sha1"],
                "language": row.get("language"),
                "supported": bool(row.get("supported")),
                "token_count": int(row.get("token_count", 0)),
                "updated_at": now,
                "last_seen_at": now,
            }
            collection.update_one(
                {"repo_name": repo_name, "file_path": file_path},
                {"$set": update, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )

    def delete_repo_file_states(self, repo_name: str, file_paths: List[str]) -> int:
        if not file_paths:
            return 0
        result = self._db[INGESTION_FILE_STATE_COLLECTION].delete_many(
            {"repo_name": repo_name, "file_path": {"$in": file_paths}}
        )
        return int(result.deleted_count)

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
        start_time = time.time()
        _validate_repo_name(repo_name)

        try:
            logger.info("Deleting all data for repo_name: %s", repo_name)

            collections = [
                CONVERSATIONS_COLLECTION,
                MESSAGES_COLLECTION,
                MENTAL_MODEL_COLLECTION,
                INGESTED_REPOS_COLLECTION,
                INGESTION_FILE_STATE_COLLECTION,
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
            logger.error("Failed to delete repo data: repo_name=%s, error=%s", repo_name, str(e))
            raise QueryError(f"Failed to delete repo data: {str(e)}") from e

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
        start_time = time.time()
        _validate_repo_name(repo_name)

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
        if not job_doc:
            return None
        return _filter_stage_metrics(_serialize_job(job_doc))
    
    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        repo_name: Optional[str] = None,
        limit: int = 50,
        skip: int = 0,
        include_total: bool = False,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], int]]:
        q: Dict[str, Any] = {}
        if status:
            q["status"] = status
        if repo_name:
            q["repo_name"] = repo_name

        projection = {
            "_id": 0,
            "job_id": 1, "repo_name": 1, "status": 1,
            "current_stage": 1, "error": 1,
            "operation": 1,
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

    def get_active_ingestion_job(self) -> Optional[Dict[str, Any]]:
        """Return the most recently updated queued/running ingestion job, if any."""
        coll = self._db[INGESTION_JOBS_COLLECTION]
        return coll.find_one(
            {"status": {"$in": ["queued", "running"]}},
            {"_id": 0},
            sort=[("updated_at", -1), ("created_at", -1)],
        )

    def cancel_active_ingestion_jobs(self, reason: str) -> int:
        """Mark any queued/running ingestion jobs as cancelled."""
        result = self._db[INGESTION_JOBS_COLLECTION].update_many(
            {"status": {"$in": ["queued", "running"]}},
            {
                "$set": {
                    "status": "cancelled",
                    "error": reason,
                    "updated_at": now_ts(),
                }
            },
        )
        return int(result.modified_count)

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
    
    def get_job(self, job_id: str) -> dict | None:
        return self._db[INGESTION_JOBS_COLLECTION].find_one({"job_id": job_id}, {"_id": 0})

    def delete_job(self, job_id: str) -> bool:
        res = self._db[INGESTION_JOBS_COLLECTION].delete_one({"job_id": job_id})
        return res.deleted_count == 1
    
    def is_repo_ingested(self, repo_name: str) -> bool:
        collection = self._db[INGESTED_REPOS_COLLECTION]
        doc = collection.find_one({"repo_name": repo_name}, {"_id": 1})
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
