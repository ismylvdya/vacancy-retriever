"""Cache of employers' IT accreditation (accreditation/resolver.py decides, this only stores)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..timeutil import from_iso, to_iso, utcnow
from .db import Database


@dataclass
class AccreditationRecord:
    key: str
    employer: str
    status: str  # yes | no | unknown
    note: str = ""
    details: dict[str, Any] = field(default_factory=dict)  # what the decision was made from (resolver.decide)
    registry_date: str = ""
    checked_at: datetime | None = None


class AccreditationRepository:
    def __init__(self, db: Database):
        self.db = db

    def get_many(self, keys: list[str]) -> dict[str, AccreditationRecord]:
        result: dict[str, AccreditationRecord] = {}
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.db.query(f"SELECT * FROM employer_accreditation WHERE key IN ({marks})", tuple(chunk)):
                result[row["key"]] = AccreditationRecord(
                    row["key"], row["employer"], row["status"], row["note"] or "",
                    json.loads(row["entities_json"] or "{}"), row["registry_date"] or "", from_iso(row["checked_at"]))
        return result

    def save(self, record: AccreditationRecord) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO employer_accreditation(key, employer, status, note, entities_json, registry_date,"
            " checked_at) VALUES (?,?,?,?,?,?,?)",
            (record.key, record.employer, record.status, record.note,
             json.dumps(record.details, ensure_ascii=False), record.registry_date,
             to_iso(record.checked_at or utcnow())),
        )

    def all(self) -> list[AccreditationRecord]:
        return list(self.get_many([r["key"] for r in self.db.query("SELECT key FROM employer_accreditation")]).values())
