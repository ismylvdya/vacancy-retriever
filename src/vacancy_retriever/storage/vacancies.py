from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..scoring.engine import Verdict
from ..scoring.facts import VacancyFacts
from ..timeutil import from_iso, to_iso, utcnow
from .db import Database

USER_STATUSES = {"new", "applied", "skipped", "not_now"}
PROCESSED_FILTERS = {"applied", "skipped", "not_now", "auto_skipped"}


@dataclass
class NewVacancy:
    primary_raw_id: int
    origin: str
    title: str
    employer: str
    category: str
    seniority: str
    decision: str
    skip_reason: str | None
    published_at: datetime | None
    facts: VacancyFacts | None = None
    verdict: Verdict | None = None
    dedup_key: str | None = None

    @property
    def user_status(self) -> str:
        if self.decision == "hidden":
            return "hidden"
        return "new" if self.decision == "show" else "auto_skipped"


@dataclass
class VacancySource:
    source_id: str
    url: str


@dataclass
class VacancyRecord:
    id: int
    title: str
    employer: str
    category: str
    seniority: str
    decision: str
    skip_reason: str | None
    group_no: int | None
    stars: int | None
    residence_permit: str | None
    residence_reason: str | None
    english_flag: bool
    informal_flag: bool
    published_at: datetime | None
    created_at: datetime | None
    user_status: str
    facts: VacancyFacts | None
    primary_raw_id: int
    origin: str
    run_id: int
    sources: list[VacancySource] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: Any) -> VacancyRecord:
        facts_json = row["facts_json"]
        return cls(
            id=row["id"],
            title=row["title"],
            employer=row["employer"] or "",
            category=row["category"] or "",
            seniority=row["seniority"] or "unknown",
            decision=row["decision"],
            skip_reason=row["skip_reason"],
            group_no=row["group_no"],
            stars=row["stars"],
            residence_permit=row["residence_permit"],
            residence_reason=row["residence_reason"],
            english_flag=bool(row["english_flag"]),
            informal_flag=bool(row["informal_flag"]),
            published_at=from_iso(row["published_at"]),
            created_at=from_iso(row["created_at"]),
            user_status=row["user_status"],
            facts=VacancyFacts.model_validate_json(facts_json) if facts_json else None,
            primary_raw_id=row["primary_raw_id"],
            origin=row["origin"],
            run_id=row["run_id"],
        )


class VacancyRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, run_id: int, vacancy: NewVacancy) -> int:
        verdict = vacancy.verdict
        cursor = self.db.execute(
            "INSERT INTO vacancies(run_id, primary_raw_id, origin, facts_json, title, employer, category, seniority,"
            " decision, skip_reason, group_no, stars, residence_permit, residence_reason, english_flag, informal_flag,"
            " published_at, created_at, user_status, dedup_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, vacancy.primary_raw_id, vacancy.origin,
                vacancy.facts.model_dump_json() if vacancy.facts else None,
                vacancy.title, vacancy.employer, vacancy.category, vacancy.seniority,
                vacancy.decision, vacancy.skip_reason,
                verdict.group if verdict else None, verdict.stars if verdict else None,
                verdict.residence_permit if verdict else None, verdict.residence_reason if verdict else None,
                int(verdict.english_flag) if verdict else 0, int(verdict.informal_flag) if verdict else 0,
                to_iso(vacancy.published_at), to_iso(utcnow()), vacancy.user_status, vacancy.dedup_key,
            ),
        )
        return int(cursor.lastrowid)

    def attach_source(self, run_id: int, vacancy_id: int, raw_item_id: int, source_id: str, url: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO vacancy_sources(vacancy_id, raw_item_id, source_id, url, run_id)"
            " VALUES (?,?,?,?,?)",
            (vacancy_id, raw_item_id, source_id, url, run_id),
        )

    def move_sources_and_delete(self, run_id: int, from_id: int, to_id: int) -> None:
        rows = self.db.query("SELECT raw_item_id, source_id, url FROM vacancy_sources WHERE vacancy_id = ?", (from_id,))
        for row in rows:
            self.attach_source(run_id, to_id, row["raw_item_id"], row["source_id"], row["url"])
        self.db.execute("DELETE FROM vacancy_sources WHERE vacancy_id = ?", (from_id,))
        self.db.execute("DELETE FROM vacancies WHERE id = ? AND staged = 1", (from_id,))

    def ids_for_raw(self, raw_item_id: int) -> list[int]:
        rows = self.db.query("SELECT DISTINCT vacancy_id FROM vacancy_sources WHERE raw_item_id = ?", (raw_item_id,))
        return [r["vacancy_id"] for r in rows]

    def get(self, vacancy_id: int) -> VacancyRecord | None:
        row = self.db.query_one("SELECT * FROM vacancies WHERE id = ?", (vacancy_id,))
        if not row:
            return None
        record = VacancyRecord.from_row(row)
        self._load_sources([record])
        return record

    def staged_for_run(self, run_id: int) -> list[VacancyRecord]:
        rows = self.db.query("SELECT * FROM vacancies WHERE run_id = ? AND staged = 1 ORDER BY id", (run_id,))
        return [VacancyRecord.from_row(r) for r in rows]

    def dedup_candidates(self, days: int) -> list[dict[str, Any]]:
        since = to_iso(utcnow() - timedelta(days=days))
        rows = self.db.query(
            "SELECT id, run_id, staged, primary_raw_id, title, employer, dedup_key, facts_json, decision FROM vacancies"
            " WHERE decision != 'hidden' AND created_at >= ?",
            (since,),
        )
        return [dict(r) for r in rows]

    def feed(self) -> list[VacancyRecord]:
        rows = self.db.query(
            "SELECT * FROM vacancies WHERE staged = 0 AND user_status = 'new'"
            " ORDER BY group_no, stars DESC, published_at DESC"
        )
        records = [VacancyRecord.from_row(r) for r in rows]
        self._load_sources(records)
        return records

    def processed(self, status_filter: str | None, limit: int = 500) -> list[VacancyRecord]:
        statuses = [status_filter] if status_filter in PROCESSED_FILTERS else sorted(PROCESSED_FILTERS)
        marks = ",".join("?" * len(statuses))
        rows = self.db.query(
            f"SELECT * FROM vacancies WHERE staged = 0 AND user_status IN ({marks})"
            " ORDER BY COALESCE(status_changed_at, created_at) DESC LIMIT ?",
            (*statuses, limit),
        )
        records = [VacancyRecord.from_row(r) for r in rows]
        self._load_sources(records)
        return records

    def processed_counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT user_status, COUNT(*) AS n FROM vacancies WHERE staged = 0 GROUP BY user_status"
        )
        return {r["user_status"]: r["n"] for r in rows}

    def set_user_status(self, vacancy_id: int, status: str) -> bool:
        if status not in USER_STATUSES:
            raise ValueError(f"Unknown status {status}")
        cursor = self.db.execute(
            "UPDATE vacancies SET user_status = ?, user_set = 1, status_changed_at = ? WHERE id = ? AND staged = 0",
            (status, to_iso(utcnow()), vacancy_id),
        )
        return cursor.rowcount > 0

    def new_in_feed_for_run(self, run_id: int) -> dict[int, int]:
        rows = self.db.query(
            "SELECT group_no, COUNT(*) AS n FROM vacancies WHERE run_id = ? AND user_status = 'new'"
            " GROUP BY group_no",
            (run_id,),
        )
        return {int(r["group_no"] or 5): r["n"] for r in rows}

    def with_facts(self) -> list[VacancyRecord]:
        rows = self.db.query("SELECT * FROM vacancies WHERE staged = 0 AND facts_json IS NOT NULL")
        return [VacancyRecord.from_row(r) for r in rows]

    def apply_recalculated(self, vacancy_id: int, verdict: Verdict, change_status: bool) -> None:
        decision = verdict.decision
        sets = (
            "decision = ?, skip_reason = ?, group_no = ?, stars = ?, residence_permit = ?, residence_reason = ?,"
            " english_flag = ?, informal_flag = ?"
        )
        params: list[Any] = [decision, verdict.skip_reason, verdict.group, verdict.stars, verdict.residence_permit,
                             verdict.residence_reason, int(verdict.english_flag), int(verdict.informal_flag)]
        if change_status:
            sets += ", user_status = ?"
            params.append({"show": "new", "skip": "auto_skipped", "hidden": "hidden"}[decision])
        self.db.execute(f"UPDATE vacancies SET {sets} WHERE id = ?", (*params, vacancy_id))

    def raw_links(self, vacancy_id: int) -> list[dict[str, Any]]:
        """Debug mode: raw items behind a vacancy — its own sources and the duplicates merged into them."""
        rows = self.db.query(
            "SELECT r.id, r.source_id, r.external_id, r.url, r.status, r.duplicate_of, r.skip_reason FROM raw_items r"
            " WHERE r.id IN (SELECT raw_item_id FROM vacancy_sources WHERE vacancy_id = ?)"
            " OR r.id = (SELECT primary_raw_id FROM vacancies WHERE id = ?)"
            " OR r.duplicate_of IN (SELECT raw_item_id FROM vacancy_sources WHERE vacancy_id = ?)"
            " ORDER BY r.id",
            (vacancy_id, vacancy_id, vacancy_id),
        )
        return [dict(r) for r in rows]

    def user_set(self, vacancy_id: int) -> bool:
        row = self.db.query_one("SELECT user_set FROM vacancies WHERE id = ?", (vacancy_id,))
        return bool(row and row["user_set"])

    def _load_sources(self, records: list[VacancyRecord]) -> None:
        if not records:
            return
        by_id = {r.id: r for r in records}
        marks = ",".join("?" * len(by_id))
        rows = self.db.query(
            f"SELECT vacancy_id, source_id, url FROM vacancy_sources WHERE vacancy_id IN ({marks}) ORDER BY id",
            tuple(by_id),
        )
        seen: set[tuple[int, str, str]] = set()
        for row in rows:
            key = (row["vacancy_id"], row["source_id"], row["url"] or "")
            if key in seen:
                continue
            seen.add(key)
            by_id[row["vacancy_id"]].sources.append(VacancySource(row["source_id"], row["url"] or ""))
