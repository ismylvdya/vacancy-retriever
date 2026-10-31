from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..collectors.base import RawItem, normalize_url
from ..timeutil import from_iso, to_iso, utcnow
from .db import Database


@dataclass
class StoredRawItem:
    id: int
    run_id: int
    staged: bool
    source_id: str
    external_id: str
    url: str
    title: str
    text: str
    detail_text: str | None
    published_at: datetime | None
    multi: bool
    meta: dict[str, Any]
    status: str
    duplicate_of: int | None

    @property
    def full_text(self) -> str:
        return self.detail_text or self.text

    @classmethod
    def from_row(cls, row: Any) -> StoredRawItem:
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            staged=bool(row["staged"]),
            source_id=row["source_id"],
            external_id=row["external_id"],
            url=row["url"] or "",
            title=row["title"] or "",
            text=row["text"],
            detail_text=row["detail_text"],
            published_at=from_iso(row["published_at"]),
            multi=bool(row["multi"]),
            meta=json.loads(row["meta_json"] or "{}"),
            status=row["status"],
            duplicate_of=row["duplicate_of"],
        )


class RawItemRepository:
    def __init__(self, db: Database):
        self.db = db

    def is_known(self, source_id: str, external_id: str) -> bool:
        row = self.db.query_one(
            "SELECT 1 FROM raw_items WHERE source_id = ? AND external_id = ?", (source_id, external_id)
        )
        return row is not None

    def known_ids(self, source_id: str) -> set[str]:
        rows = self.db.query("SELECT external_id FROM raw_items WHERE source_id = ?", (source_id,))
        return {r["external_id"] for r in rows}

    def insert(self, run_id: int, item: RawItem, status: str, duplicate_of: int | None = None,
               skip_reason: str | None = None) -> int | None:
        meta = {**item.meta, "detail_url": item.detail_url} if item.detail_url else item.meta
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO raw_items(run_id, source_id, external_id, url, norm_url, title, text, text_hash,"
            " published_at, collected_at, multi, meta_json, status, duplicate_of, skip_reason, detail_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, item.source_id, item.external_id, item.url, normalize_url(item.url) if item.url else None,
             item.title, item.text, item.text_hash, to_iso(item.published_at), to_iso(utcnow()), int(item.multi),
             json.dumps(meta, ensure_ascii=False), status, duplicate_of, skip_reason,
             item.meta.get("detail_text")),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def hidden(self, limit: int = 2000) -> list[tuple[StoredRawItem, str]]:
        """Debug mode: ads and other "not a vacancy" posts — hidden by the prefilter or by the LLM."""
        rows = self.db.query(
            "SELECT * FROM raw_items r WHERE staged = 0 AND (status = 'hidden' OR (status = 'scored'"
            " AND NOT EXISTS (SELECT 1 FROM vacancy_sources s WHERE s.raw_item_id = r.id)"
            " AND NOT EXISTS (SELECT 1 FROM vacancies v WHERE v.primary_raw_id = r.id)))"
            " ORDER BY run_id DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [(StoredRawItem.from_row(r), r["skip_reason"] or "Not a vacancy (LLM)") for r in rows]

    def get(self, raw_id: int) -> StoredRawItem | None:
        row = self.db.query_one("SELECT * FROM raw_items WHERE id = ?", (raw_id,))
        return StoredRawItem.from_row(row) if row else None

    def set_status(self, raw_id: int, status: str, skip_reason: str | None = None) -> None:
        self.db.execute("UPDATE raw_items SET status = ?, skip_reason = ? WHERE id = ?", (status, skip_reason, raw_id))

    def set_detail(self, raw_id: int, detail_text: str) -> None:
        self.db.execute("UPDATE raw_items SET detail_text = ? WHERE id = ?", (detail_text, raw_id))

    def defer(self, raw_id: int, after: date, today: date) -> None:
        """This run's item waits for another way to get its description (status waiting_page: not scored, not in
        the feed) — not before `after`."""
        self.db.execute(
            "UPDATE raw_items SET status = 'waiting_page',"
            " meta_json = json_set(COALESCE(meta_json, '{}'), '$.page_after', ?, '$.deferred_on', ?) WHERE id = ?",
            (after.isoformat(), today.isoformat(), raw_id))

    def waiting_pages(self) -> list[StoredRawItem]:
        rows = self.db.query("SELECT * FROM raw_items WHERE staged = 0 AND status = 'waiting_page' ORDER BY id")
        return [StoredRawItem.from_row(r) for r in rows]

    def count_waiting_pages(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM raw_items WHERE staged = 0 AND status = 'waiting_page'")
        return int(row["n"]) if row else 0

    def mark_page_tried(self, raw_id: int) -> None:
        """A waiting item had its turn (found or not): the next run only evaluates it (e.g. after an LLM failure).
        Like detail_text, this enrichment is written at once — a cancelled run must not ask LinkedIn again."""
        self.db.execute("UPDATE raw_items SET meta_json = json_set(COALESCE(meta_json, '{}'), '$.page_tried', 1)"
                        " WHERE id = ?", (raw_id,))

    def by_run(self, run_id: int, status: str | None = None) -> list[StoredRawItem]:
        sql = "SELECT * FROM raw_items WHERE run_id = ? AND staged = 1"
        params: tuple = (run_id,)
        if status:
            sql += " AND status = ?"
            params += (status,)
        return [StoredRawItem.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def pending_from_previous_runs(self) -> list[StoredRawItem]:
        rows = self.db.query("SELECT * FROM raw_items WHERE staged = 0 AND status = 'pending_llm' ORDER BY id")
        return [StoredRawItem.from_row(r) for r in rows]

    def count_pending(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM raw_items WHERE staged = 0 AND status = 'pending_llm'")
        return int(row["n"]) if row else 0

    def find_by_norm_url(self, norm_url: str, exclude_source: str | None = None) -> int | None:
        row = self.db.query_one(
            "SELECT id, duplicate_of FROM raw_items WHERE norm_url = ? AND (? IS NULL OR source_id != ?)"
            " ORDER BY id LIMIT 1",
            (norm_url, exclude_source, exclude_source),
        )
        return (row["duplicate_of"] or row["id"]) if row else None

    def find_by_hash(self, text_hash: str) -> int | None:
        row = self.db.query_one(
            "SELECT id, duplicate_of FROM raw_items WHERE text_hash = ? ORDER BY id LIMIT 1", (text_hash,)
        )
        return (row["duplicate_of"] or row["id"]) if row else None

    def recent_texts(self, days: int, min_length: int) -> list[tuple[int, str, str]]:
        since = to_iso(utcnow() - timedelta(days=days))
        rows = self.db.query(
            "SELECT id, source_id, text, duplicate_of FROM raw_items WHERE collected_at >= ? AND length(text) > ?"
            " AND status != 'duplicate'",
            (since, min_length),
        )
        return [(r["duplicate_of"] or r["id"], r["source_id"], r["text"]) for r in rows]

    def recent_titled(self, days: int) -> list[dict[str, Any]]:
        """Items of the last days for the exact title dedup: id, source, title, start of the text, company, multi."""
        since = to_iso(utcnow() - timedelta(days=days))
        rows = self.db.query(
            "SELECT id, source_id, title, substr(text, 1, 400) AS head, json_extract(meta_json, '$.company') AS company,"
            " multi, status, run_id FROM raw_items WHERE collected_at >= ? AND status NOT IN ('duplicate', 'hidden')",
            (since,),
        )
        return [dict(r) for r in rows]

    def make_duplicate(self, raw_id: int, original_id: int) -> None:
        """An item already stored becomes a duplicate of a better one (a channel post of a site vacancy)."""
        self.db.execute("UPDATE raw_items SET status = 'duplicate', duplicate_of = ? WHERE id = ?", (original_id, raw_id))
        self.db.execute("UPDATE raw_items SET duplicate_of = ? WHERE duplicate_of = ?", (original_id, raw_id))

    def duplicates_of(self, raw_id: int) -> list[StoredRawItem]:
        rows = self.db.query("SELECT * FROM raw_items WHERE duplicate_of = ?", (raw_id,))
        return [StoredRawItem.from_row(r) for r in rows]
