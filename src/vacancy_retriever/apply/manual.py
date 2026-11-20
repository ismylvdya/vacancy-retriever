"""A vacancy the user found himself (decision of the user 20.11.2026): «+ Add vacancy» in the sidebar.

The user copies from the site the title, the link and the description (all three required) and, optionally, the
company. Nothing is parsed or guessed: no page is opened, no LLM call is made for it (user: no tokens on reading the
site). It becomes an ordinary vacancy of the source «manual» (never in the feed, status Applied), so the Apply
pipeline — the Obsidian note, the Drive folder, the CV, the cover letter, the sidebar row with Stop / Retry — works for
it unchanged. Without facts, «👾где» and the grade in the names stay empty: the note's other parts are as usual.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

from ..collectors.base import RawItem
from ..storage import Storage
from ..storage.vacancies import NewVacancy
from ..timeutil import to_iso, utcnow
from .models import Application, ApplicationState

SOURCE_ID = "manual"


class ManualError(ValueError):
    """Bad input of the «Add vacancy» window (shown to the user as is)."""


def check_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ManualError("Paste the link of the vacancy (https://…)")
    return url


@dataclass
class ManualVacancy:
    vacancy_id: int
    application: Application


class ManualIntake:
    """«Add vacancy» → a vacancy of the source «manual» + its application (the caller queues it with the boxes)."""

    def __init__(self, storage: Storage):
        self._storage = storage

    def create(self, title: str, url: str, description: str, company: str = "") -> ManualVacancy:
        title, description, company = (title or "").strip(), (description or "").strip(), (company or "").strip()
        if not title:
            raise ManualError("Type the vacancy's title")
        url = check_url(url)
        if not description:
            raise ManualError("Paste the vacancy's description")
        now = utcnow()
        digest = hashlib.sha1(f"{url}\n{now.isoformat()}".encode()).hexdigest()[:16]
        item = RawItem(SOURCE_ID, f"manual:{digest}", url, description, now, title=title,
                       meta={"manual": True, "company": company or None})
        db = self._storage.db
        with db.transaction():
            raw_id = self._storage.items.insert(0, item, "scored")
            assert raw_id is not None
            vacancy = NewVacancy(raw_id, "manual", title, company, "other", "unknown", "show", None, now)
            vacancy_id = self._storage.vacancies.insert(0, vacancy)
            self._storage.vacancies.attach_source(0, vacancy_id, raw_id, SOURCE_ID, url)
            db.execute("UPDATE raw_items SET staged = 0 WHERE id = ?", (raw_id,))
            db.execute("UPDATE vacancies SET staged = 0, user_status = 'applied', user_set = 1, status_changed_at = ?"
                       " WHERE id = ?", (to_iso(now), vacancy_id))
            db.execute("UPDATE vacancy_sources SET staged = 0 WHERE vacancy_id = ?", (vacancy_id,))
        # An instant in the purgatory state: ApplicationService.generate queues it with the boxes (the same path as
        # Generate in the sidebar).
        application = self._storage.applications.create(vacancy_id, now, "apply", ApplicationState.DRAFT)
        return ManualVacancy(vacancy_id, application)
