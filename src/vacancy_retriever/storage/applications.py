from __future__ import annotations

from datetime import datetime
from typing import Any

from ..apply.models import Application, ApplicationState
from ..timeutil import to_iso, utcnow
from .db import Database

_RESULT_FIELDS = {
    "state", "step", "due_at", "note_path", "folder_id", "folder_url", "materials_json", "pdf_path", "pdf_file_id",
    "pdf_url", "letter", "skills", "error", "warning", "cancel_requested", "dismissed", "kind", "note_mine",
    "stats_json", "with_cv", "with_letter",
}


class ApplicationRepository:
    def __init__(self, db: Database):
        self.db = db

    def get(self, application_id: int) -> Application | None:
        row = self.db.query_one("SELECT * FROM applications WHERE id = ?", (application_id,))
        return Application.from_row(row) if row else None

    def for_vacancy(self, vacancy_id: int) -> Application | None:
        row = self.db.query_one("SELECT * FROM applications WHERE vacancy_id = ?", (vacancy_id,))
        return Application.from_row(row) if row else None

    def create(self, vacancy_id: int, due_at: datetime, kind: str = "apply",
               state: ApplicationState = ApplicationState.QUEUED) -> Application:
        now = to_iso(utcnow())
        cursor = self.db.execute(
            "INSERT INTO applications(vacancy_id, state, due_at, created_at, updated_at, kind)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (vacancy_id, state, to_iso(due_at), now, now, kind),
        )
        application = self.get(int(cursor.lastrowid or 0))
        assert application is not None
        return application

    def update(self, application_id: int, **fields: Any) -> None:
        unknown = set(fields) - _RESULT_FIELDS
        if unknown:
            raise ValueError(f"Unknown application fields: {sorted(unknown)}")
        if not fields:
            return
        values = [to_iso(v) if isinstance(v, datetime) else v for v in fields.values()]
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE applications SET {assignments}, updated_at = ? WHERE id = ?",
                        (*values, to_iso(utcnow()), application_id))

    def delete(self, application_id: int) -> None:
        self.db.execute("DELETE FROM applications WHERE id = ?", (application_id,))

    def next_due(self, now: datetime) -> Application | None:
        row = self.db.query_one(
            "SELECT * FROM applications WHERE state IN (?, ?) AND due_at <= ? ORDER BY due_at, id LIMIT 1",
            (ApplicationState.QUEUED, ApplicationState.WAITING, to_iso(now)),
        )
        return Application.from_row(row) if row else None

    def history(self, limit: int = 300) -> list[Application]:
        """Every application, newest first (Statistics → Applies)."""
        return [Application.from_row(r) for r in self.db.query("SELECT * FROM applications ORDER BY id DESC LIMIT ?",
                                                               (limit,))]

    def active_vacancy_ids(self) -> set[int]:
        """Vacancies whose application is queued, in work or waiting for the Claude limit."""
        rows = self.db.query("SELECT vacancy_id FROM applications WHERE state IN (?, ?, ?)",
                             (ApplicationState.QUEUED, ApplicationState.WORKING, ApplicationState.WAITING))
        return {r["vacancy_id"] for r in rows}

    def visible(self) -> list[Application]:
        # every «▶» and N: in work in Purgatory, done in Generated (user, 20.11.2026)
        rows = self.db.query("SELECT * FROM applications WHERE dismissed = 0 ORDER BY id DESC")
        return [Application.from_row(r) for r in rows]

    def recover(self) -> int:
        """After a restart: work that was interrupted goes back to the queue (finished steps are kept)."""
        cursor = self.db.execute(
            "UPDATE applications SET state = ?, updated_at = ? WHERE state = ?",
            (ApplicationState.QUEUED, to_iso(utcnow()), ApplicationState.WORKING),
        )
        return cursor.rowcount
