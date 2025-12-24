import sqlite3
import json
import time
from pathlib import Path
from typing import List, Dict, Optional, Set


class Cache:
    """
    Simple SQLite-backed cache for per-file reference mappings.

    - Namespaced by analyzer/server combo (namespace).
    - Stores file SHA-1s and per-reference mapping blobs.
    - Also maintains an index from definition file -> reference locations,
      so we can invalidate mappings that point into a changed file.
    """

    def __init__(self, repo_path: Path, namespace: str):
        self.repo_path = repo_path
        self.namespace = namespace
        self.db_path = self.repo_path / ".lsp_ref_cache.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()
        self._ensure_namespace()

    # ---------- schema / namespace ----------

    def _init_db(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key   TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
              namespace TEXT NOT NULL,
              path      TEXT NOT NULL,
              sha1      TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(namespace, path)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
              namespace  TEXT NOT NULL,
              ref_path   TEXT NOT NULL,
              ref_line   INTEGER NOT NULL,
              ref_column INTEGER NOT NULL,
              data       TEXT NOT NULL,
              PRIMARY KEY(namespace, ref_path, ref_line, ref_column)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mappings_ref_path
            ON mappings(namespace, ref_path)
            """
        )
        # Index from definition file -> (reference location)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS def_index (
              namespace  TEXT NOT NULL,
              def_path   TEXT NOT NULL,
              ref_path   TEXT NOT NULL,
              ref_line   INTEGER NOT NULL,
              ref_column INTEGER NOT NULL,
              PRIMARY KEY(namespace, def_path, ref_path, ref_line, ref_column)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_def_index_def_path
            ON def_index(namespace, def_path)
            """
        )
        self.conn.commit()

    def _ensure_namespace(self):
        cur = self.conn.execute("SELECT value FROM meta WHERE key = 'namespace'")
        row = cur.fetchone()
        if not row or row[0] != self.namespace:
            # Soft-reset this namespace only
            self.conn.execute("DELETE FROM files WHERE namespace = ?", (self.namespace,))
            self.conn.execute("DELETE FROM mappings WHERE namespace = ?", (self.namespace,))
            self.conn.execute("DELETE FROM def_index WHERE namespace = ?", (self.namespace,))
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('namespace', ?)",
                (self.namespace,),
            )
            self.conn.commit()

    # ---------- file SHA helpers ----------

    def get_file_sha(self, rel_path: str) -> Optional[str]:
        cur = self.conn.execute(
            "SELECT sha1 FROM files WHERE namespace = ? AND path = ?",
            (self.namespace, rel_path),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def update_file_sha(self, rel_path: str, sha1: str):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO files(namespace, path, sha1, updated_at)
            VALUES(?, ?, ?, ?)
            """,
            (self.namespace, rel_path, sha1, int(time.time())),
        )

    # ---------- mappings (per reference) ----------

    def delete_mappings_for_file(self, rel_path: str):
        """
        Delete all mappings where this file is the reference file.
        Also removes corresponding rows from def_index where this
        file is the reference (ref_path).
        """
        self.conn.execute(
            "DELETE FROM mappings WHERE namespace = ? AND ref_path = ?",
            (self.namespace, rel_path),
        )
        self.conn.execute(
            "DELETE FROM def_index WHERE namespace = ? AND ref_path = ?",
            (self.namespace, rel_path),
        )

    def load_mappings_for_file(self, rel_path: str) -> List[Dict]:
        cur = self.conn.execute(
            "SELECT data FROM mappings WHERE namespace = ? AND ref_path = ?",
            (self.namespace, rel_path),
        )
        return [json.loads(row[0]) for row in cur.fetchall()]

    def store_mapping(self, mapping: Dict):
        """
        Store mapping and update def_index for its definition locations.
        """
        ref = mapping.get("reference") or {}
        ref_path = ref.get("file_path")
        line = ref.get("line")
        col = ref.get("column")
        if ref_path is None or line is None or col is None:
            return
        data = json.dumps(mapping)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO mappings(namespace, ref_path, ref_line, ref_column, data)
            VALUES(?, ?, ?, ?, ?)
            """,
            (self.namespace, ref_path, int(line), int(col), data),
        )

        defs = mapping.get("definitions") or []
        # Use a set to avoid duplicate def_path entries for same ref location
        seen_defs: Set[str] = set()
        for d in defs:
            def_path = d.get("file_path")
            if not def_path:
                continue
            if def_path in seen_defs:
                continue
            seen_defs.add(def_path)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO def_index(namespace, def_path, ref_path, ref_line, ref_column)
                VALUES(?, ?, ?, ?, ?)
                """,
                (self.namespace, def_path, ref_path, int(line), int(col)),
            )

    def invalidate_by_definition_file(self, def_path: str) -> List[str]:
        """
        Given a definition file path, remove all mappings whose definitions
        point into this file.

        Returns:
            List of reference file paths whose mappings were invalidated.
            These ref files should be re-resolved in the current run.
        """
        cur = self.conn.execute(
            """
            SELECT ref_path, ref_line, ref_column
            FROM def_index
            WHERE namespace = ? AND def_path = ?
            """,
            (self.namespace, def_path),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        ref_paths: Set[str] = set()
        for ref_path, ref_line, ref_col in rows:
            ref_paths.add(ref_path)
            self.conn.execute(
                """
                DELETE FROM mappings
                WHERE namespace = ? AND ref_path = ? AND ref_line = ? AND ref_column = ?
                """,
                (self.namespace, ref_path, int(ref_line), int(ref_col)),
            )

        # Now remove the def_index entries for this def_path
        self.conn.execute(
            "DELETE FROM def_index WHERE namespace = ? AND def_path = ?",
            (self.namespace, def_path),
        )

        return list(ref_paths)

    # ---------- lifecycle ----------

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.conn.commit()
        finally:
            self.conn.close()
