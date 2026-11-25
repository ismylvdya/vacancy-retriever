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
    created_at TEXT NOT NULL,
    application_id INTEGER,            -- the Apply / Not now call (Statistics → Applies)
    five_hour REAL,                    -- Claude subscription after the call: 5-hour limit used, 0–1
    seven_day REAL,                    -- … weekly limit used, 0–1
    five_hour_resets_at INTEGER        -- when that 5-hour window resets (unix time)
);
CREATE INDEX IF NOT EXISTS ix_llm_run ON llm_calls(run_id);

-- Debug mode: the user's complaints about how a vacancy was evaluated (several per vacancy are fine).
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reports_vacancy ON reports(vacancy_id);

-- Apply sidebar: one row per vacancy sent with ▶. Each step stores its result; a stored result = the step is done.
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    vacancy_id INTEGER NOT NULL UNIQUE REFERENCES vacancies(id) ON DELETE CASCADE,
    state TEXT NOT NULL,               -- draft | queued | working | waiting | failed | done | cancelled
    step TEXT,                         -- the step being done (or that failed)
    due_at TEXT NOT NULL,              -- not before this time (Undo window, retry after a Claude limit)
    note_path TEXT,
    folder_id TEXT,
    folder_url TEXT,
    materials_json TEXT,
    pdf_path TEXT,
    pdf_file_id TEXT,
    pdf_url TEXT,
    letter TEXT,
    skills TEXT,
    error TEXT,
    warning TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'apply', -- apply (▶) | not_now (N: only the Obsidian note with the skills)
    note_mine TEXT,                    -- what the app last wrote into «### моё» (user edits are kept)
    stats_json TEXT                    -- {"steps": {step: seconds}} for Statistics → Applies
);
CREATE INDEX IF NOT EXISTS ix_applications_state ON applications(state, due_at);

-- Statuses of applications from e-mails (rejection, next stage, test…): the Statuses tab. Not linked to vacancies.
CREATE TABLE IF NOT EXISTS mail_statuses (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    staged INTEGER NOT NULL DEFAULT 1,
    raw_item_id INTEGER,
    source_id TEXT NOT NULL,           -- the mailbox source (gmail | yandex)
    message_id TEXT,
    received_at TEXT,
    sender TEXT,
    subject TEXT,
    company TEXT,
    position TEXT,
    stage TEXT NOT NULL,               -- see mail/statuses.py → STAGES
    summary TEXT,
    confidence TEXT,
    unsure INTEGER NOT NULL DEFAULT 0, -- the LLM is not sure this is a real status (maybe spam / not about a job)
    seen_at TEXT,                      -- when the user first saw it on the Statuses tab (NULL = new)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mail_statuses_run ON mail_statuses(run_id, staged);

-- Unread letters already checked and left alone (no job words / not about a job): not fetched again.
CREATE TABLE IF NOT EXISTS mail_checked (
    source_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    verdict TEXT NOT NULL,             -- no_job_words
    checked_at TEXT NOT NULL,
    PRIMARY KEY (source_id, message_id)
);

-- IT accreditation of employers (register of the Ministry of Digital Development), cached by the employer's name.
CREATE TABLE IF NOT EXISTS employer_accreditation (
    key TEXT PRIMARY KEY,              -- normalised employer name (accreditation/resolver.py → employer_key)
    employer TEXT NOT NULL,
    status TEXT NOT NULL,              -- yes | no | unknown
    note TEXT,                         -- the legal entity / INN it was decided by, or why it is unknown
    entities_json TEXT,
    registry_date TEXT,                -- snapshot of the register used
    checked_at TEXT NOT NULL
);
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
        added = {("source_runs", "seen", "INTEGER"), ("vacancies", "user_note", "TEXT"),
                 ("runs", "real_limit_pct", "REAL"), ("llm_calls", "application_id", "INTEGER"),
                 ("llm_calls", "five_hour", "REAL"), ("llm_calls", "seven_day", "REAL"),
                 ("llm_calls", "five_hour_resets_at", "INTEGER"),
                 ("applications", "kind", "TEXT NOT NULL DEFAULT 'apply'"), ("applications", "note_mine", "TEXT"),
                 ("applications", "stats_json", "TEXT"),
                 ("applications", "with_cv", "INTEGER NOT NULL DEFAULT 1"),
                 ("applications", "with_letter", "INTEGER NOT NULL DEFAULT 1"),
                 ("applications", "generation", "INTEGER NOT NULL DEFAULT 1"),
                 ("applications", "provided_pdf", "TEXT"), ("applications", "provided_letter", "TEXT"),
                 ("applications", "with_note", "INTEGER NOT NULL DEFAULT 1")}  # unused since 19.11.2026: the note is always made
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
