from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,              -- running | committed | cancelled | failed
    pid INTEGER,
    options_json TEXT,
    summary_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL,              -- ok | error | truncated | not_configured | disabled
    collected INTEGER DEFAULT 0,
    seen INTEGER,
    passed INTEGER DEFAULT 0,
    to_feed INTEGER DEFAULT 0,
    duration_s REAL DEFAULT 0,
    error TEXT,
    alert TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_runs_run ON source_runs(run_id);

CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    staged INTEGER NOT NULL DEFAULT 1,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    norm_url TEXT,
    title TEXT,
    text TEXT NOT NULL,
    detail_text TEXT,
    text_hash TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    multi INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT,
    status TEXT NOT NULL,              -- pending_llm | scored | prefiltered | duplicate | hidden
    duplicate_of INTEGER,
    skip_reason TEXT,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_raw_status ON raw_items(status);
CREATE INDEX IF NOT EXISTS ix_raw_norm_url ON raw_items(norm_url);
CREATE INDEX IF NOT EXISTS ix_raw_hash ON raw_items(text_hash);
CREATE INDEX IF NOT EXISTS ix_raw_run ON raw_items(run_id, staged);

CREATE TABLE IF NOT EXISTS raw_status_updates (
    run_id INTEGER NOT NULL,
    raw_item_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, raw_item_id)
);

CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    staged INTEGER NOT NULL DEFAULT 1,
    primary_raw_id INTEGER NOT NULL,
    origin TEXT NOT NULL,              -- llm | prefilter
    facts_json TEXT,
    title TEXT NOT NULL,
    employer TEXT,
    category TEXT,
    seniority TEXT,
    decision TEXT NOT NULL,            -- show | skip | hidden
    skip_reason TEXT,
    group_no INTEGER,
    stars INTEGER,
    residence_permit TEXT,
    residence_reason TEXT,
    english_flag INTEGER DEFAULT 0,
    informal_flag INTEGER DEFAULT 0,
    published_at TEXT,
    created_at TEXT NOT NULL,
    user_status TEXT NOT NULL,         -- new | applied | skipped | not_now | auto_skipped | hidden
    user_set INTEGER NOT NULL DEFAULT 0,
    status_changed_at TEXT,
    dedup_key TEXT
);
CREATE INDEX IF NOT EXISTS ix_vac_status ON vacancies(user_status, staged);
CREATE INDEX IF NOT EXISTS ix_vac_run ON vacancies(run_id, staged);
CREATE INDEX IF NOT EXISTS ix_vac_dedup ON vacancies(dedup_key);

CREATE TABLE IF NOT EXISTS vacancy_sources (
    id INTEGER PRIMARY KEY,
    vacancy_id INTEGER NOT NULL,
    raw_item_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    run_id INTEGER NOT NULL,
    staged INTEGER NOT NULL DEFAULT 1,
    UNIQUE(vacancy_id, raw_item_id)
);
CREATE INDEX IF NOT EXISTS ix_vs_vac ON vacancy_sources(vacancy_id);
CREATE INDEX IF NOT EXISTS ix_vs_raw ON vacancy_sources(raw_item_id);

CREATE TABLE IF NOT EXISTS source_cursors (
    source_id TEXT PRIMARY KEY,
    last_id TEXT,
    last_published_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS run_cursors (
    run_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    last_id TEXT,
    last_published_at TEXT,
    PRIMARY KEY (run_id, source_id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    run_id INTEGER,
    stage TEXT NOT NULL,               -- scoring | scoring_email | dedup
    provider TEXT NOT NULL,
    model TEXT,
    n_items INTEGER DEFAULT 0,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    tokens_unknown INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    duration_s REAL,
    auth_source TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_run ON llm_calls(run_id);
"""


class Database:
    """SQLite in WAL mode; one connection per thread."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Columns added after v0 databases were created. Add new ones here, never drop."""
        added = {("source_runs", "seen", "INTEGER")}
        for table, column, kind in added:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        yield self._connection()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._connection().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self._connection().execute(sql, params)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
