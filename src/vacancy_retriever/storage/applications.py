from __future__ import annotations

from datetime import datetime
from typing import Any

from ..apply.models import Application, ApplicationState
from ..timeutil import to_iso, utcnow
from .db import Database

_RESULT_FIELDS = {
    "state", "step", "due_at", "note_path", "folder_id", "folder_url", "materials_json", "pdf_path", "pdf_file_id",
    "pdf_url", "letter", "skills", "error", "warning", "cancel_requested", "dismissed", "kind", "note_mine",
    "stats_json", "with_cv", "with_letter", "provided_pdf", "provided_letter",
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

    def stop(self, application_id: int) -> bool:
        cursor = self.db.execute(
            "UPDATE applications SET cancel_requested = (state = ?),"
            " state = CASE WHEN state = ? THEN state ELSE ? END, updated_at = ?"
            " WHERE id = ? AND dismissed = 0 AND state IN (?, ?, ?, ?)",
            (ApplicationState.WORKING, ApplicationState.WORKING, ApplicationState.CANCELLED, to_iso(utcnow()),
             application_id, ApplicationState.QUEUED, ApplicationState.WORKING,
             ApplicationState.WAITING, ApplicationState.FAILED),
        )
        return cursor.rowcount > 0

    def restart(self, application_id: int, due_at: datetime, kind: str, state: ApplicationState) -> None:
        now = to_iso(utcnow())
        self.db.execute(
            "UPDATE applications SET generation = generation + 1, kind = ?, state = ?, due_at = ?,"
            " created_at = ?, updated_at = ?, step = NULL, note_path = NULL, note_mine = NULL,"
            " folder_id = NULL, folder_url = NULL, materials_json = NULL, pdf_path = NULL, pdf_file_id = NULL,"
            " pdf_url = NULL, letter = NULL, provided_pdf = NULL, provided_letter = NULL,"
            " skills = NULL, error = NULL, warning = NULL, stats_json = NULL,"
            " cancel_requested = 0, dismissed = 0, with_cv = 1, with_letter = 1 WHERE id = ?",
            (kind, state, to_iso(due_at), now, now, application_id),
        )

    def next_due(self, now: datetime) -> Application | None:
        row = self.db.query_one(
            "SELECT * FROM applications WHERE dismissed = 0 AND state IN (?, ?) AND due_at <= ?"
            " ORDER BY due_at, id LIMIT 1",
            (ApplicationState.QUEUED, ApplicationState.WAITING, to_iso(now)),
        )
        return Application.from_row(row) if row else None

    def claim_due(self, now: datetime) -> Application | None:
        with self.db.transaction():
            app = self.next_due(now)
            if app is None:
                return None
            self.update(app.id, state=ApplicationState.WORKING)
            return self.get(app.id)

    def history(self, limit: int = 300) -> list[Application]:
        """Every application, newest first (Statistics → Applies)."""
        return [Application.from_row(r) for r in self.db.query("SELECT * FROM applications ORDER BY id DESC LIMIT ?",
                                                               (limit,))]

    def active_vacancy_ids(self) -> set[int]:
        """Vacancies whose application is queued, in work or waiting for the Claude limit."""
        rows = self.db.query("SELECT vacancy_id FROM applications WHERE dismissed = 0 AND state IN (?, ?, ?)",
                             (ApplicationState.QUEUED, ApplicationState.WORKING, ApplicationState.WAITING))
        return {r["vacancy_id"] for r in rows}

    def visible(self) -> list[Application]:
        # every «▶» and N: in work in Purgatory, done in Generated (user, 20.11.2026)
        rows = self.db.query(
            "SELECT a.* FROM applications a JOIN vacancies v ON v.id = a.vacancy_id"
            " WHERE a.dismissed = 0 AND v.staged = 0 AND v.user_status <> 'new'"
            " ORDER BY a.created_at DESC, a.id DESC"
        )
        return [Application.from_row(r) for r in rows]

    def recover(self) -> int:
        """After a restart: work that was interrupted goes back to the queue (finished steps are kept)."""
        cursor = self.db.execute(
            "UPDATE applications SET state = CASE WHEN dismissed = 1 THEN ? ELSE ? END, updated_at = ?"
            " WHERE state = ?",
            (ApplicationState.CANCELLED, ApplicationState.QUEUED, to_iso(utcnow()), ApplicationState.WORKING),
        )
        return cursor.rowcount
