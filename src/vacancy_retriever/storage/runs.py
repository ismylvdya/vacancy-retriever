from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..collectors.base import SourceCursor
from ..timeutil import from_iso, to_iso, utcnow
from .db import Database

STAGED_TABLES = ("raw_items", "vacancies", "vacancy_sources", "mail_statuses")


@dataclass
class SourceRunRecord:
    source_id: str
    status: str
    collected: int = 0
    seen: int | None = None
    passed: int = 0
    to_feed: int = 0
    duration_s: float = 0.0
    error: str | None = None
    alert: str | None = None
    note: str | None = None


class RunRepository:
    """Run lifecycle: start, per-source health, cursors, atomic commit and rollback."""

    def __init__(self, db: Database):
        self.db = db

    def start(self, options: dict[str, Any]) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs(started_at, status, pid, options_json) VALUES (?, 'running', ?, ?)",
            (to_iso(utcnow()), os.getpid(), json.dumps(options, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    def get(self, run_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return dict(row) if row else None

    def running(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM runs WHERE status = 'running'")]

    def last_finished(self) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM runs WHERE status IN ('committed', 'failed') ORDER BY id DESC LIMIT 1"
        )
        return dict(row) if row else None

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    # ---- missed periods: a run whose period starts after the previous run leaves a hole nobody will collect
    #      (cursors move on). Kept per source until included in a run or dismissed (user, 28.10.2026).
    MISSED_KEY = "missed_periods"

    def last_collected(self, source_id: str, before_run: int) -> datetime | None:
        """Start of the latest committed run, before `before_run`, that collected this source."""
        row = self.db.query_one(
            "SELECT r.started_at FROM source_runs s JOIN runs r ON r.id = s.run_id WHERE s.source_id = ?"
            " AND s.run_id < ? AND r.status = 'committed' AND s.status IN ('ok', 'truncated')"
            " ORDER BY r.id DESC LIMIT 1", (source_id, before_run))
        return from_iso(row["started_at"]) if row else None

    def missed(self) -> dict[str, tuple[datetime, datetime]]:
        row = self.db.query_one("SELECT value FROM meta WHERE key = ?", (self.MISSED_KEY,))
        try:
            data = json.loads(row["value"]) if row else {}
        except ValueError:
            data = {}
        return {source: (from_iso(a), from_iso(b)) for source, (a, b) in data.items()}

    def _save_missed(self, periods: dict[str, tuple[datetime, datetime]]) -> None:
        value = json.dumps({s: [to_iso(a), to_iso(b)] for s, (a, b) in periods.items()})
        self.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (self.MISSED_KEY, value))

    def add_missed(self, source_id: str, start: datetime, end: datetime) -> None:
        periods = self.missed()
        if source_id in periods:  # holes of one source merge into one span
            start, end = min(start, periods[source_id][0]), max(end, periods[source_id][1])
        periods[source_id] = (start, end)
        self._save_missed(periods)

    def clear_missed(self, source_ids: list[str] | None = None) -> None:
        periods = self.missed()
        self._save_missed({} if source_ids is None else {s: p for s, p in periods.items() if s not in source_ids})

    def record_source(self, run_id: int, record: SourceRunRecord) -> None:
        self.db.execute(
            "INSERT INTO source_runs(run_id, source_id, status, collected, seen, passed, to_feed, duration_s, error,"
            " alert, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, record.source_id, record.status, record.collected, record.seen, record.passed, record.to_feed,
             round(record.duration_s, 1), record.error, record.alert, record.note),
        )

    def update_source_counts(self, run_id: int, source_id: str, passed: int, to_feed: int) -> None:
        self.db.execute(
            "UPDATE source_runs SET passed = ?, to_feed = ? WHERE run_id = ? AND source_id = ?",
            (passed, to_feed, run_id, source_id),
        )

    def source_runs(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM source_runs WHERE run_id = ? ORDER BY id", (run_id,))
        return [dict(r) for r in rows]

    def cursor(self, source_id: str) -> SourceCursor:
        row = self.db.query_one("SELECT * FROM source_cursors WHERE source_id = ?", (source_id,))
        if not row:
            return SourceCursor()
        return SourceCursor(last_id=row["last_id"], last_published_at=from_iso(row["last_published_at"]))

    def has_cursor(self, source_id: str) -> bool:
        return self.db.query_one("SELECT 1 FROM source_cursors WHERE source_id = ?", (source_id,)) is not None

    def stage_cursor(self, run_id: int, source_id: str, cursor: SourceCursor) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO run_cursors(run_id, source_id, last_id, last_published_at) VALUES (?,?,?,?)",
            (run_id, source_id, cursor.last_id, to_iso(cursor.last_published_at)),
        )

    def stage_raw_status(self, run_id: int, raw_item_id: int, status: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO raw_status_updates(run_id, raw_item_id, status) VALUES (?,?,?)",
            (run_id, raw_item_id, status),
        )

    def commit(self, run_id: int, summary: dict[str, Any], max_age_days: int, now: datetime | None = None) -> None:
        """One short transaction: staged rows become visible, cursors move, expired cards leave the feed."""
        now = now or utcnow()
        expire_before = to_iso(now - timedelta(days=max_age_days))
        with self.db.transaction() as conn:
            for table in STAGED_TABLES:
                conn.execute(f"UPDATE {table} SET staged = 0 WHERE run_id = ? AND staged = 1", (run_id,))
            conn.execute(
                "UPDATE raw_items SET status = (SELECT u.status FROM raw_status_updates u"
                " WHERE u.run_id = ? AND u.raw_item_id = raw_items.id)"
                " WHERE id IN (SELECT raw_item_id FROM raw_status_updates WHERE run_id = ?)",
                (run_id, run_id),
            )
            conn.execute("DELETE FROM raw_status_updates WHERE run_id = ?", (run_id,))
            conn.execute(
                "INSERT OR REPLACE INTO source_cursors(source_id, last_id, last_published_at, updated_at)"
                " SELECT rc.source_id, rc.last_id, rc.last_published_at, ? FROM run_cursors rc"
                " JOIN source_runs sr ON sr.run_id = rc.run_id AND sr.source_id = rc.source_id"
                " WHERE rc.run_id = ? AND sr.status IN ('ok', 'truncated')",
                (to_iso(now), run_id),
            )
            conn.execute("DELETE FROM run_cursors WHERE run_id = ?", (run_id,))
            conn.execute(
                "UPDATE vacancies SET user_status = 'auto_skipped', skip_reason = 'Expired', status_changed_at = ?"
                " WHERE user_status = 'new' AND staged = 0 AND published_at IS NOT NULL AND published_at < ?",
                (to_iso(now), expire_before),
            )
            conn.execute(
                "UPDATE runs SET status = 'committed', finished_at = ?, summary_json = ? WHERE id = ?",
                (to_iso(now), json.dumps(summary, ensure_ascii=False), run_id),
            )

    def rollback(self, run_id: int, status: str = "cancelled", error: str | None = None) -> None:
        """Remove everything the run wrote; cursors untouched; token records kept and marked."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM vacancy_sources WHERE staged = 1 AND (run_id = ? OR vacancy_id IN"
                " (SELECT id FROM vacancies WHERE run_id = ? AND staged = 1))",
                (run_id, run_id),
            )
            conn.execute("DELETE FROM vacancies WHERE run_id = ? AND staged = 1", (run_id,))
            conn.execute("DELETE FROM mail_statuses WHERE run_id = ? AND staged = 1", (run_id,))
            conn.execute("DELETE FROM raw_items WHERE run_id = ? AND staged = 1", (run_id,))
            for table in ("raw_status_updates", "run_cursors"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            if status == "cancelled":
                conn.execute("DELETE FROM source_runs WHERE run_id = ?", (run_id,))
            conn.execute("UPDATE llm_calls SET cancelled = 1 WHERE run_id = ?", (run_id,))
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, to_iso(utcnow()), error, run_id),
            )

    def recover_interrupted(self) -> list[int]:
        """Runs left in 'running' (crash, sleep, kill) are treated as cancelled."""
        recovered = []
        for run in self.running():
            self.rollback(run["id"], status="cancelled", error="Interrupted (app was stopped or crashed)")
            recovered.append(run["id"])
        return recovered
