"""Statuses of the user's applications found in e-mails (the Statuses tab). They are written staged with the run
and become visible at its commit, like vacancies; they are never linked to a vacancy (decision 20.11.2026)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..timeutil import from_iso, to_iso, utcnow
from .db import Database


@dataclass
class NewStatus:
    source_id: str
    stage: str
    raw_item_id: int | None = None
    message_id: str = ""
    received_at: datetime | None = None
    sender: str = ""
    subject: str = ""
    company: str = ""
    position: str = ""
    summary: str = ""
    confidence: str = ""
    unsure: bool = False


@dataclass
class StatusRecord(NewStatus):
    id: int = 0
    seen_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> StatusRecord:
        return cls(
            id=row["id"], source_id=row["source_id"], stage=row["stage"], raw_item_id=row["raw_item_id"],
            message_id=row["message_id"] or "", received_at=from_iso(row["received_at"]), sender=row["sender"] or "",
            subject=row["subject"] or "", company=row["company"] or "", position=row["position"] or "",
            summary=row["summary"] or "", confidence=row["confidence"] or "", unsure=bool(row["unsure"]),
            seen_at=from_iso(row["seen_at"]), created_at=from_iso(row["created_at"]),
        )


class StatusRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, run_id: int, status: NewStatus) -> int:
        cursor = self.db.execute(
            "INSERT INTO mail_statuses(run_id, staged, raw_item_id, source_id, message_id, received_at, sender, subject,"
            " company, position, stage, summary, confidence, unsure, created_at) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, status.raw_item_id, status.source_id, status.message_id, to_iso(status.received_at),
             status.sender, status.subject, status.company, status.position, status.stage, status.summary,
             status.confidence, int(status.unsure), to_iso(utcnow())),
        )
        return int(cursor.lastrowid)

    def visible(self, limit: int = 1000) -> list[StatusRecord]:
        rows = self.db.query(
            "SELECT * FROM mail_statuses WHERE staged = 0"
            " ORDER BY COALESCE(received_at, created_at) DESC, id DESC LIMIT ?", (limit,))
        return [StatusRecord.from_row(r) for r in rows]

    def new_count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM mail_statuses WHERE staged = 0 AND seen_at IS NULL")
        return row["n"] if row else 0

    def mark_seen(self, ids: list[int], now: datetime | None = None) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cursor = self.db.execute(
            f"UPDATE mail_statuses SET seen_at = ? WHERE seen_at IS NULL AND staged = 0 AND id IN ({marks})",
            (to_iso(now or utcnow()), *ids))
        return cursor.rowcount

    def mark_unseen(self, status_id: int) -> bool:
        cursor = self.db.execute("UPDATE mail_statuses SET seen_at = NULL WHERE id = ? AND staged = 0", (status_id,))
        return cursor.rowcount > 0

    # ---------------------------------------------------------------- letters already checked
    def checked(self, source_id: str) -> set[str]:
        rows = self.db.query("SELECT message_id FROM mail_checked WHERE source_id = ?", (source_id,))
        return {r["message_id"] for r in rows}

    def remember_checked(self, source_id: str, message_ids: list[str], verdict: str) -> None:
        now = to_iso(utcnow())
        for message_id in message_ids:
            self.db.execute("INSERT OR IGNORE INTO mail_checked(source_id, message_id, verdict, checked_at)"
                            " VALUES (?,?,?,?)", (source_id, message_id, verdict, now))
