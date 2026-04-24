import json
import logging
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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

INGESTION_JOBS_TABLE = "ingestion_jobs"
INGESTED_REPOS_TABLE = "ingested_repos"
CONVERSATIONS_TABLE = "conversations"
MESSAGES_TABLE = "messages"
MENTAL_MODEL_TABLE = "mental_model"
INGESTION_FILE_STATE_TABLE = "ingestion_file_state"

_KNOWN_TABLES = {
    INGESTION_JOBS_TABLE,
    INGESTED_REPOS_TABLE,
    CONVERSATIONS_TABLE,
    MESSAGES_TABLE,
    MENTAL_MODEL_TABLE,
    INGESTION_FILE_STATE_TABLE,
}


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
        "supported_tokens",
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


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _json_loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _conversation_id() -> str:
    return secrets.token_hex(12)


def _valid_conversation_id(conversation_id: str) -> bool:
    return isinstance(conversation_id, str) and len(conversation_id) == 24 and all(
        c in "0123456789abcdef" for c in conversation_id
    )


def _parse_dt(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


class SQLiteTableAccessor:
    """Compatibility wrapper for legacy table-style reads."""

    def __init__(self, client: "SQLiteClient", table_name: str):
        self.client = client
        self.table_name = table_name

    def find_one(self, query: Optional[dict] = None, projection: Optional[dict] = None, sort: Optional[list] = None):
        rows = self.client._find_documents(self.table_name, query or {}, projection, sort=sort, limit=1)
        return rows[0] if rows else None

    def find(self, query: Optional[dict] = None, projection: Optional[dict] = None):
        return self.client._find_documents(self.table_name, query or {}, projection)

    def count_documents(self, query: Optional[dict] = None) -> int:
        return len(self.client._find_documents(self.table_name, query or {}, None))


class SQLiteClient:
    """
    SQLite database client wrapper.

    Some public method names intentionally match the previous database client
    so endpoint and ingestion code can migrate without a broad rewrite.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(SQLiteClient, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[str] = None, *args, **kwargs):
        if hasattr(self, "_initialized") and self._initialized:
            return

        self._db_path = db_path or Config.SQLITE_DB_PATH
        if not self._db_path:
            raise InvalidConnectionStringError("SQLITE_DB_PATH not configured")
        self._conn: sqlite3.Connection | None = None
        self._thread_lock = threading.RLock()
        self._initialized = True

    def connect(self, db_name: Optional[str] = None):
        _ = db_name
        try:
            if self._db_path != ":memory:":
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self._db_path,
                timeout=30,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
            logger.info("Connected to SQLite database: %s", self._db_path)
        except Exception as e:
            logger.error("Failed to connect to SQLite database %s: %s", self._db_path, str(e))
            raise DBConnectionError(f"Failed to connect to database: {str(e)}") from e

    def __getitem__(self, table_name: str):
        if table_name not in _KNOWN_TABLES:
            raise KeyError(table_name)
        return SQLiteTableAccessor(self, table_name)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DBConnectionError("Database client is not connected")
        return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._thread_lock:
            conn = self._require_conn()
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._thread_lock:
            return list(self._require_conn().execute(sql, params))

    def _create_schema(self) -> None:
        conn = self._require_conn()
        with self._thread_lock:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    repo_name TEXT NOT NULL,
                    title TEXT,
                    type TEXT NOT NULL DEFAULT 'REPO_CHAT',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    repo_name TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    stage TEXT,
                    status TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    job_id TEXT PRIMARY KEY,
                    repo_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT,
                    stages_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT,
                    operation TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingested_repos (
                    repo_name TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingestion_file_state (
                    repo_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha1 TEXT NOT NULL,
                    language TEXT,
                    supported INTEGER NOT NULL,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(repo_name, file_path)
                );

                CREATE TABLE IF NOT EXISTS mental_model (
                    repo_name TEXT NOT NULL,
                    file_path TEXT NOT NULL DEFAULT '',
                    document_type TEXT NOT NULL,
                    data TEXT,
                    context TEXT,
                    sha1 TEXT,
                    PRIMARY KEY(repo_name, file_path, document_type)
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_repo_updated
                    ON conversations(repo_name, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
                    ON messages(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_status_updated
                    ON ingestion_jobs(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_repo_updated
                    ON ingestion_jobs(repo_name, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mental_model_repo_type
                    ON mental_model(repo_name, document_type);
                """
            )
            conn.commit()

    def get_brief_file_overview(self, repo_name: str, file_path: str) -> str:
        row = self._query(
            """
            SELECT data FROM mental_model
            WHERE repo_name = ? AND document_type = 'BRIEF_FILE_OVERVIEW' AND file_path = ?
            """,
            (repo_name, file_path),
        )
        return row[0]["data"] if row else ""

    def delete_brief_file_overview(self, repo_name: str, file_path: str) -> bool:
        cursor = self._execute(
            """
            DELETE FROM mental_model
            WHERE repo_name = ? AND document_type = 'BRIEF_FILE_OVERVIEW' AND file_path = ?
            """,
            (repo_name, file_path),
        )
        return cursor.rowcount > 0

    def get_critical_file_paths(self, repo_name: str) -> List[str]:
        rows = self._query(
            """
            SELECT file_path FROM mental_model
            WHERE repo_name = ? AND document_type = 'BRIEF_FILE_OVERVIEW'
            ORDER BY file_path
            """,
            (repo_name,),
        )
        return [row["file_path"] for row in rows if row["file_path"]]

    def get_repo_file_states(self, repo_name: str) -> Dict[str, Dict[str, Any]]:
        rows = self._query(
            """
            SELECT file_path, sha1, language, supported, token_count
            FROM ingestion_file_state
            WHERE repo_name = ?
            """,
            (repo_name,),
        )
        return {
            row["file_path"]: {
                "file_path": row["file_path"],
                "sha1": row["sha1"],
                "language": row["language"],
                "supported": bool(row["supported"]),
                "token_count": row["token_count"],
            }
            for row in rows
            if row["file_path"]
        }

    def upsert_repo_file_states(self, repo_name: str, rows: List[Dict[str, Any]]) -> None:
        now = now_ts()
        with self._thread_lock:
            conn = self._require_conn()
            conn.executemany(
                """
                INSERT INTO ingestion_file_state (
                    repo_name, file_path, sha1, language, supported, token_count,
                    created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_name, file_path) DO UPDATE SET
                    sha1 = excluded.sha1,
                    language = excluded.language,
                    supported = excluded.supported,
                    token_count = excluded.token_count,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at
                """,
                [
                    (
                        repo_name,
                        row["file_path"],
                        row["sha1"],
                        row.get("language"),
                        1 if row.get("supported") else 0,
                        int(row.get("token_count", 0)),
                        now,
                        now,
                        now,
                    )
                    for row in rows
                ],
            )
            conn.commit()

    def delete_repo_file_states(self, repo_name: str, file_paths: List[str]) -> int:
        if not file_paths:
            return 0
        placeholders = ",".join("?" for _ in file_paths)
        cursor = self._execute(
            f"DELETE FROM ingestion_file_state WHERE repo_name = ? AND file_path IN ({placeholders})",
            (repo_name, *file_paths),
        )
        return int(cursor.rowcount)

    def delete_repo_data(self, repo_name: str) -> Dict[str, Any]:
        start_time = time.time()
        _validate_repo_name(repo_name)

        try:
            total_deleted = 0
            with self._thread_lock:
                conn = self._require_conn()
                for table in [
                    CONVERSATIONS_TABLE,
                    MESSAGES_TABLE,
                    MENTAL_MODEL_TABLE,
                    INGESTED_REPOS_TABLE,
                    INGESTION_FILE_STATE_TABLE,
                    INGESTION_JOBS_TABLE,
                ]:
                    cursor = conn.execute(f"DELETE FROM {table} WHERE repo_name = ?", (repo_name,))
                    total_deleted += cursor.rowcount
                conn.commit()

            return {
                "repo_name": repo_name,
                "collections_processed": 6,
                "total_deleted": total_deleted,
                "duration_ms": (time.time() - start_time) * 1000,
            }
        except Exception as e:
            logger.error("Failed to delete repo data: repo_name=%s, error=%s", repo_name, str(e))
            raise QueryError(f"Failed to delete repo data: {str(e)}") from e

    def create_conversation(self, repo_name: str) -> dict:
        _validate_repo_name(repo_name)
        try:
            conversation_id = _conversation_id()
            timestamp = now_ts()
            self._execute(
                """
                INSERT INTO conversations (id, repo_name, created_at, updated_at, type)
                VALUES (?, ?, ?, ?, 'REPO_CHAT')
                """,
                (conversation_id, repo_name, timestamp, timestamp),
            )
            return {
                "conversation_id": conversation_id,
                "repo_name": repo_name,
                "created_at": _parse_dt(timestamp),
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
        params: list[Any] = []
        where = ""
        if repo_name:
            where = "WHERE repo_name = ?"
            params.append(repo_name)
        params.extend([limit, offset])
        rows = self._query(
            f"""
            SELECT id, repo_name, title, created_at, updated_at
            FROM conversations
            {where}
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )
        return [
            {
                "_id": row["id"],
                "repo_name": row["repo_name"],
                "title": row["title"],
                "created_at": _parse_dt(row["created_at"]),
                "updated_at": _parse_dt(row["updated_at"]),
            }
            for row in rows
        ]

    def add_ingested_repo(self, repo_name: str, job_id: str):
        self._execute(
            """
            INSERT OR IGNORE INTO ingested_repos (repo_name, job_id, ingested_at)
            VALUES (?, ?, ?)
            """,
            (repo_name, job_id, datetime.now(timezone.utc).isoformat()),
        )

    def upsert_ingestion_job(
        self,
        job: IngestionJobStatus,
        *,
        error: dict | str | None = None,
        extra_fields: dict | None = None,
    ):
        existing = self._query("SELECT stages_json FROM ingestion_jobs WHERE job_id = ?", (job.job_id,))
        stages = _json_loads(existing[0]["stages_json"], {}) if existing else {}
        for stage, payload in job.stage_status.items():
            stages[stage.value] = payload.value if hasattr(payload, "value") else payload

        operation = extra_fields.get("operation") if extra_fields else None
        now = now_ts()
        self._execute(
            """
            INSERT INTO ingestion_jobs (
                job_id, repo_name, status, current_stage, stages_json, error_json,
                operation, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                repo_name = excluded.repo_name,
                status = excluded.status,
                current_stage = excluded.current_stage,
                stages_json = excluded.stages_json,
                error_json = excluded.error_json,
                operation = COALESCE(excluded.operation, ingestion_jobs.operation),
                updated_at = excluded.updated_at
            """,
            (
                job.job_id,
                job.repo_name,
                job.status,
                job.current_stage.value,
                _json_dumps(stages),
                _json_dumps(error) if error is not None else None,
                operation,
                now,
                now,
            ),
        )

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        job_doc = self.get_job(job_id)
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
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if repo_name:
            clauses.append("repo_name = ?")
            params.append(repo_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = self._query(
            f"""
            SELECT job_id, repo_name, status, current_stage, error_json, operation, created_at, updated_at
            FROM ingestion_jobs
            {where}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, skip),
        )
        jobs = [self._job_row_to_dict(row, include_stages=False) for row in rows]

        if include_total:
            total_row = self._query(f"SELECT COUNT(*) AS count FROM ingestion_jobs {where}", tuple(params))
            return jobs, int(total_row[0]["count"])

        return jobs

    def get_active_ingestion_job(self) -> Optional[Dict[str, Any]]:
        rows = self._query(
            """
            SELECT * FROM ingestion_jobs
            WHERE status IN ('queued', 'running')
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """
        )
        return self._job_row_to_dict(rows[0]) if rows else None

    def cancel_active_ingestion_jobs(self, reason: str) -> int:
        cursor = self._execute(
            """
            UPDATE ingestion_jobs
            SET status = 'cancelled', error_json = ?, updated_at = ?
            WHERE status IN ('queued', 'running')
            """,
            (_json_dumps(reason), now_ts()),
        )
        return int(cursor.rowcount)

    def list_ingested_repos(self) -> List[str]:
        rows = self._query("SELECT repo_name FROM ingested_repos ORDER BY repo_name")
        return [row["repo_name"] for row in rows]

    def conversation_exists(self, conversation_id: str) -> bool:
        if not _valid_conversation_id(conversation_id):
            raise ValueError("Invalid conversation id")
        row = self._query("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        return bool(row)

    def list_conversation_messages(
        self,
        *,
        conversation_id: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        rows = self._query(
            """
            SELECT role, content, message_type, stage, status, metadata_json, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (conversation_id, limit),
        )
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "message_type": row["message_type"],
                "stage": row["stage"],
                "status": row["status"],
                "metadata": _json_loads(row["metadata_json"], {}),
                "created_at": _parse_dt(row["created_at"]),
            }
            for row in rows
        ]

    def delete_conversation(self, conversation_id: str) -> None:
        if not _valid_conversation_id(conversation_id):
            raise ValueError("Invalid conversation id")
        if not self.conversation_exists(conversation_id):
            raise KeyError("Conversation not found")
        self._execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def get_job(self, job_id: str) -> dict | None:
        rows = self._query("SELECT * FROM ingestion_jobs WHERE job_id = ?", (job_id,))
        return self._job_row_to_dict(rows[0]) if rows else None

    def delete_job(self, job_id: str) -> bool:
        cursor = self._execute("DELETE FROM ingestion_jobs WHERE job_id = ?", (job_id,))
        return cursor.rowcount == 1

    def is_repo_ingested(self, repo_name: str) -> bool:
        row = self._query("SELECT repo_name FROM ingested_repos WHERE repo_name = ?", (repo_name,))
        return bool(row)

    def persist_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        message_type: str,
        stage: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[datetime] = None,
    ) -> None:
        timestamp = (created_at or datetime.now(timezone.utc)).isoformat()
        conv = self.get_conversation(conversation_id)
        if not conv:
            raise KeyError("Conversation not found")
        self._execute(
            """
            INSERT INTO messages (
                conversation_id, repo_name, role, content, message_type, stage,
                status, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                conv["repo_name"],
                role,
                content,
                message_type,
                stage,
                status,
                _json_dumps(metadata or {}),
                timestamp,
            ),
        )
        self._execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id))

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        if not _valid_conversation_id(conversation_id):
            raise ValueError("Invalid conversation id")
        rows = self._query(
            "SELECT id, repo_name, title, type, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "_id": row["id"],
            "repo_name": row["repo_name"],
            "title": row["title"],
            "type": row["type"],
            "created_at": _parse_dt(row["created_at"]),
            "updated_at": _parse_dt(row["updated_at"]),
        }

    def list_chat_history(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT role, content
            FROM messages
            WHERE conversation_id = ? AND message_type != 'progress_event'
            ORDER BY created_at ASC, id ASC
            """,
            (conversation_id,),
        )
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def get_repo_context(self, repo_name: str) -> str:
        rows = self._query(
            """
            SELECT context FROM mental_model
            WHERE repo_name = ? AND document_type = 'REPO_CONTEXT' AND file_path = ''
            """,
            (repo_name,),
        )
        return rows[0]["context"] if rows else ""

    def upsert_repo_context(self, repo_name: str, context: str) -> None:
        self._execute(
            """
            INSERT INTO mental_model (repo_name, file_path, document_type, context)
            VALUES (?, '', 'REPO_CONTEXT', ?)
            ON CONFLICT(repo_name, file_path, document_type) DO UPDATE SET
                context = excluded.context
            """,
            (repo_name, context),
        )

    def find_mental_model_document(
        self,
        *,
        repo_name: str,
        file_path: Optional[str] = None,
        document_types: Optional[list[str]] = None,
        sha1: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        docs = self.list_mental_model_documents(
            repo_name=repo_name,
            file_path=file_path,
            document_types=document_types,
            sha1=sha1,
            limit=1,
        )
        return docs[0] if docs else None

    def list_mental_model_documents(
        self,
        *,
        repo_name: str,
        file_path: Optional[str] = None,
        document_types: Optional[list[str]] = None,
        sha1: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Dict[str, Any]]:
        clauses = ["repo_name = ?"]
        params: list[Any] = [repo_name]
        if file_path is not None:
            clauses.append("file_path = ?")
            params.append(file_path)
        if document_types:
            placeholders = ",".join("?" for _ in document_types)
            clauses.append(f"document_type IN ({placeholders})")
            params.extend(document_types)
        if sha1 is not None:
            clauses.append("sha1 = ?")
            params.append(sha1)
        limit_sql = "LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        rows = self._query(
            f"""
            SELECT repo_name, file_path, document_type, data, context, sha1
            FROM mental_model
            WHERE {' AND '.join(clauses)}
            ORDER BY file_path, document_type
            {limit_sql}
            """,
            tuple(params),
        )
        return [
            {
                "repo_name": row["repo_name"],
                "file_path": row["file_path"],
                "document_type": row["document_type"],
                "data": row["data"],
                "context": row["context"],
                "sha1": row["sha1"],
            }
            for row in rows
        ]

    def upsert_mental_model_document(
        self,
        *,
        repo_name: str,
        file_path: str,
        document_type: str,
        data: str,
        sha1: Optional[str] = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO mental_model (repo_name, file_path, document_type, data, sha1)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repo_name, file_path, document_type) DO UPDATE SET
                data = excluded.data,
                sha1 = excluded.sha1
            """,
            (repo_name, file_path, document_type, data, sha1),
        )

    def delete_mental_model_documents(
        self,
        *,
        repo_name: str,
        file_paths: Optional[list[str]] = None,
        document_types: Optional[list[str]] = None,
    ) -> int:
        clauses = ["repo_name = ?"]
        params: list[Any] = [repo_name]
        if file_paths is not None:
            if not file_paths:
                return 0
            placeholders = ",".join("?" for _ in file_paths)
            clauses.append(f"file_path IN ({placeholders})")
            params.extend(file_paths)
        if document_types:
            placeholders = ",".join("?" for _ in document_types)
            clauses.append(f"document_type IN ({placeholders})")
            params.extend(document_types)
        cursor = self._execute(
            f"DELETE FROM mental_model WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return int(cursor.rowcount)

    def count_mental_model_documents(self, *, repo_name: str, document_type: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS count FROM mental_model WHERE repo_name = ? AND document_type = ?",
            (repo_name, document_type),
        )
        return int(rows[0]["count"])

    def health_check(self) -> Dict[str, Any]:
        start_time = time.time()

        if self._conn is None:
            return {
                "status": "unhealthy",
                "error": "Client not initialized or database not selected",
                "response_time_ms": 0.0,
            }

        try:
            self._query("SELECT 1")
            tables = [
                row["name"]
                for row in self._query(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                "status": "healthy",
                "response_time_ms": (time.time() - start_time) * 1000,
                "database": self._db_path,
                "collections": tables,
                "collection_count": len(tables),
            }
        except Exception as e:
            logger.error("SQLite health check failed: %s", str(e))
            return {
                "status": "unhealthy",
                "error": str(e),
                "response_time_ms": (time.time() - start_time) * 1000,
            }

    def close(self):
        if self._conn is None:
            logger.warning("close() called but client was not initialized")
            return

        try:
            logger.info("Closing SQLite connection")
            with self._thread_lock:
                self._conn.close()
        except Exception as e:
            logger.error("Error closing SQLite connection: %s", str(e))
            raise
        finally:
            self._conn = None

    def _job_row_to_dict(self, row: sqlite3.Row, *, include_stages: bool = True) -> Dict[str, Any]:
        result = {
            "job_id": row["job_id"],
            "repo_name": row["repo_name"],
            "status": row["status"],
            "current_stage": row["current_stage"],
            "error": _json_loads(row["error_json"], row["error_json"]),
            "operation": row["operation"],
            "created_at": _parse_dt(row["created_at"]),
            "updated_at": _parse_dt(row["updated_at"]),
        }
        if include_stages and "stages_json" in row.keys():
            result["stages"] = _json_loads(row["stages_json"], {})
        return result

    def _find_documents(
        self,
        table_name: str,
        query: dict[str, Any],
        projection: Optional[dict[str, int]],
        *,
        sort: Optional[list] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        if table_name == MENTAL_MODEL_TABLE:
            return self._find_mental_model_compat(query, projection, limit=limit)
        if table_name == CONVERSATIONS_TABLE:
            conversation_id = query.get("_id") or query.get("id")
            if conversation_id:
                doc = self.get_conversation(str(conversation_id))
                return [self._project(doc, projection)] if doc else []
        raise NotImplementedError(f"Collection compatibility is not implemented for {table_name}")

    def _find_mental_model_compat(
        self,
        query: dict[str, Any],
        projection: Optional[dict[str, int]],
        *,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        document_types = None
        doc_type_query = query.get("document_type")
        if isinstance(doc_type_query, dict) and "$in" in doc_type_query:
            document_types = doc_type_query["$in"]
        elif doc_type_query:
            document_types = [doc_type_query]
        docs = self.list_mental_model_documents(
            repo_name=query["repo_name"],
            file_path=query.get("file_path"),
            document_types=document_types,
            sha1=query.get("sha1"),
            limit=limit,
        )
        return [self._project(doc, projection) for doc in docs]

    def _project(self, doc: Optional[dict[str, Any]], projection: Optional[dict[str, int]]) -> Optional[dict[str, Any]]:
        if doc is None or projection is None:
            return doc
        if projection == {"_id": 0}:
            return {k: v for k, v in doc.items() if k != "_id"}
        projected = {k: doc[k] for k, keep in projection.items() if keep and k in doc}
        if projection.get("_id", 1) and "_id" in doc:
            projected["_id"] = doc["_id"]
        return projected


def init_db_client():
    client = SQLiteClient()
    client.connect()
    return client


def get_db_client() -> SQLiteClient:
    if SQLiteClient._instance is None:
        init_db_client()
    return SQLiteClient._instance
