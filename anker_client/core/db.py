"""SQLite persistence (catalog index, per-user library data, download queue, wishlist).

One file (``AppPaths.database_file``), WAL mode, one connection per thread.
Schema changes are append-only migrations in ``_MIGRATIONS``; never edit a
migration that has shipped.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    -- Listing index of the whole store (crawled from /games pages).
    CREATE TABLE IF NOT EXISTS catalog (
        slug          TEXT PRIMARY KEY,
        title         TEXT NOT NULL,
        title_norm    TEXT NOT NULL,           -- core.paths.normalize_title(title)
        cover_url     TEXT NOT NULL DEFAULT '',
        primary_genre TEXT NOT NULL DEFAULT '',
        year          INTEGER,
        size_text     TEXT NOT NULL DEFAULT '',
        size_bytes    INTEGER,
        listing_rank  INTEGER,                 -- position in newest-first order at last full sync
        first_seen    TEXT NOT NULL,
        last_seen     TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_catalog_title_norm ON catalog(title_norm);
    CREATE INDEX IF NOT EXISTS idx_catalog_rank ON catalog(listing_rank);

    -- Cached GameDetails JSON per slug.
    CREATE TABLE IF NOT EXISTS game_details (
        slug       TEXT PRIMARY KEY,
        json       TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );

    -- Per-user data for installed games (the manifest on disk is the source of truth
    -- for install facts; this table holds user state and cached computed values).
    CREATE TABLE IF NOT EXISTS installs (
        install_id       TEXT PRIMARY KEY,
        path             TEXT NOT NULL,
        slug             TEXT NOT NULL DEFAULT '',
        title            TEXT NOT NULL DEFAULT '',
        favorite         INTEGER NOT NULL DEFAULT 0,
        hidden           INTEGER NOT NULL DEFAULT 0,
        playtime_seconds INTEGER NOT NULL DEFAULT 0,
        last_played      TEXT NOT NULL DEFAULT '',
        size_bytes       INTEGER,
        size_checked_at  TEXT NOT NULL DEFAULT '',
        latest_version   TEXT NOT NULL DEFAULT '',
        update_available INTEGER NOT NULL DEFAULT 0,
        update_checked_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_installs_slug ON installs(slug);

    CREATE TABLE IF NOT EXISTS play_sessions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        install_id  TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        ended_at    TEXT NOT NULL,
        seconds     INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_install ON play_sessions(install_id);

    -- Persistent download queue (DownloadJob JSON).
    CREATE TABLE IF NOT EXISTS jobs (
        id         TEXT PRIMARY KEY,
        state      TEXT NOT NULL,
        position   INTEGER NOT NULL,
        json       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_position ON jobs(position);

    CREATE TABLE IF NOT EXISTS wishlist (
        slug      TEXT PRIMARY KEY,
        title     TEXT NOT NULL,
        cover_url TEXT NOT NULL DEFAULT '',
        added_at  TEXT NOT NULL
    );
    """,
]


class Database:
    """Thread-safe facade over a single SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    # --- connections -----------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def close(self) -> None:
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        self._local = threading.local()

    # --- queries -----------------------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self._connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self._connect().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row is not None and row[0] is not None else default

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        """Run one write statement; returns ``rowcount``."""
        with self._write_lock:
            return self._connect().execute(sql, params).rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any] | dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.executemany(sql, rows)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (``BEGIN IMMEDIATE``…``COMMIT``/``ROLLBACK``)."""
        with self._write_lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # --- meta key/value ------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str | None) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- migrations ----------------------------------------------------------------------
    @property
    def schema_version(self) -> int:
        return int(self._connect().execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> None:
        with self._write_lock:
            conn = self._connect()
            current = int(conn.execute("PRAGMA user_version").fetchone()[0])
            for index, script in enumerate(_MIGRATIONS[current:], start=current + 1):
                log.info("Applying database migration %d", index)
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {index};\nCOMMIT;")
