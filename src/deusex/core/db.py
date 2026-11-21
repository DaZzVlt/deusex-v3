from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any

logger = logging.getLogger(__name__)

_MIGRATION_V1_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        model_used TEXT NOT NULL DEFAULT '',
        fast_path INTEGER NOT NULL DEFAULT 0,
        sources_json TEXT NOT NULL DEFAULT '[]',
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        pinned INTEGER NOT NULL DEFAULT 0,
        source_message_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        fire_at TEXT NOT NULL,
        recurrence TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routines (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        trigger_phrase TEXT NOT NULL DEFAULT '',
        icon TEXT NOT NULL DEFAULT 'sparkle',
        steps_json TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS action_audit (
        id TEXT PRIMARY KEY,
        tool_name TEXT NOT NULL,
        module_id TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        status TEXT NOT NULL,
        args_json TEXT NOT NULL DEFAULT '{}',
        undo_plan_json TEXT,
        undone_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_aliases (
        device_uid TEXT PRIMARY KEY,
        alias TEXT NOT NULL,
        device_type TEXT NOT NULL,
        last_ip TEXT NOT NULL DEFAULT '',
        mac_address TEXT NOT NULL DEFAULT '',
        port INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_ledger (
        day_utc TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        fast_path_hits INTEGER NOT NULL DEFAULT 0,
        cloud_calls INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day_utc, provider_id, model_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_catalog_cache (
        provider_id TEXT PRIMARY KEY,
        models_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS local_media_index (
        path TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        artist TEXT NOT NULL DEFAULT '',
        album TEXT NOT NULL DEFAULT '',
        duration_sec REAL NOT NULL DEFAULT 0.0,
        mtime REAL NOT NULL DEFAULT 0.0,
        indexed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paired_devices (
        device_id TEXT PRIMARY KEY,
        device_name TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT 'android',
        token_hash TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_conv_created ON messages (conversation_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_cat_pinned ON memories (category, pinned DESC, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_status_fire ON scheduled_jobs (status, fire_at)",
    "CREATE INDEX IF NOT EXISTS idx_action_audit_created ON action_audit (created_at DESC)",
)

_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "Esquema inicial DeusEx v3 (WAL)", _MIGRATION_V1_SQL),
)


class Database:
    """Conexión SQLite 3 única en modo WAL protegida por RLock con transacciones explícitas."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._tx_depth = 0
        self._closed = False

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def is_open(self) -> bool:
        return self._conn is not None and not self._closed

    def open(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            self._conn = conn
            self._closed = False

            self._apply_pragmas_locked(conn)
            self._run_migrations_locked(conn)

    def _require_conn_locked(self) -> sqlite3.Connection:
        if self._conn is None or self._closed:
            raise RuntimeError("La base de datos SQLite no está abierta.")
        return self._conn

    @staticmethod
    def _apply_pragmas_locked(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA temp_store = MEMORY")

    def _run_migrations_locked(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied_rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        applied_versions = {int(row["version"]) for row in applied_rows}

        for version, description, statements in _MIGRATIONS:
            if version in applied_versions:
                continue
            with self.transaction():
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                    (version, description, datetime.now(UTC).isoformat()),
                )
            logger.info("Migración SQLite v%d aplicada: %s", version, description)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transacción explícita segura con soporte de anidamiento mediante SAVEPOINT."""
        with self._lock:
            conn = self._require_conn_locked()
            depth = self._tx_depth
            savepoint_name = f"deusex_sp_{depth}"
            if depth == 0:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute(f"SAVEPOINT {savepoint_name}")
            self._tx_depth += 1
            try:
                yield conn
            except BaseException:
                self._tx_depth -= 1
                if depth == 0:
                    conn.execute("ROLLBACK")
                else:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                raise
            else:
                self._tx_depth -= 1
                if depth == 0:
                    conn.execute("COMMIT")
                else:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            conn = self._require_conn_locked()
            if self._tx_depth > 0:
                cursor = conn.execute(sql, params)
                return cursor.rowcount
            with self.transaction() as tx_conn:
                cursor = tx_conn.execute(sql, params)
                return cursor.rowcount

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> int:
        with self._lock:
            conn = self._require_conn_locked()
            if self._tx_depth > 0:
                cursor = conn.executemany(sql, seq_of_params)
                return cursor.rowcount
            with self.transaction() as tx_conn:
                cursor = tx_conn.executemany(sql, seq_of_params)
                return cursor.rowcount

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            conn = self._require_conn_locked()
            row: sqlite3.Row | None = conn.execute(sql, params).fetchone()
            return row

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            conn = self._require_conn_locked()
            return list(conn.execute(sql, params).fetchall())

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.fetchone(sql, params)
        if row is None:
            return None
        return row[0]

    def schema_version(self) -> int:
        val = self.scalar("SELECT MAX(version) FROM schema_migrations")
        return int(val) if val is not None else 0

    def journal_mode(self) -> str:
        val = self.scalar("PRAGMA journal_mode")
        return str(val).lower() if val is not None else ""

    def integrity_ok(self) -> bool:
        val = self.scalar("PRAGMA quick_check")
        return str(val).lower() == "ok"

    def close(self) -> None:
        with self._lock:
            if self._conn is None or self._closed:
                return
            conn = self._conn
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:
                logger.warning("No se pudo ejecutar wal_checkpoint(TRUNCATE): %s", exc)
            finally:
                try:
                    conn.close()
                finally:
                    self._conn = None
                    self._tx_depth = 0
                    self._closed = True
