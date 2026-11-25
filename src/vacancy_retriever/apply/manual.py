"""A vacancy the user found himself (decision of the user 20.11.2026): «+ Add vacancy» in the sidebar.

The user enters the title, link, description and optional company. The vacancy is added as Applied without opening
or scoring the source page. The Apply pipeline creates the Obsidian note with any requested or provided materials.
Without scored facts, «👾где» and the grade in the names stay empty.
"""

from __future__ import annotations

import hashlib
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.parse import urlparse
from uuid import uuid4

from ..collectors.base import RawItem
from ..storage import Storage
from ..storage.vacancies import NewVacancy
from ..timeutil import to_iso, utcnow
from .models import Application, ApplicationState

SOURCE_ID = "manual"


class ManualError(ValueError):
    """Bad input of the «Add vacancy» window (shown to the user as is)."""

def choose_pdf() -> Path | None:
    script = """use scripting additions
try
    activate
    return POSIX path of (choose file with prompt "Choose a CV (PDF)" of type {"com.adobe.pdf"})
on error number -128
    return ""
end try"""
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManualError("The macOS file chooser could not open. Try again.") from exc
    if result.returncode:
        raise ManualError("The macOS file chooser could not open. Try again.")
    selected = result.stdout.removesuffix("\n")
    return Path(selected) if selected else None


def validate_pdf(path: Path) -> None:
    if path.suffix.lower() == ".pdf" and path.is_file():
        with suppress(OSError):
            with path.open("rb") as file:
                if file.read(5) == b"%PDF-":
                    return
    raise ManualError("Choose a readable PDF file.")


class ManualCvPicker:
    """Let the web form refer to files chosen on this Mac without accepting arbitrary local paths."""

    def __init__(self) -> None:
        self._selected: dict[str, tuple[Path, float]] = {}
        self._lock = Lock()

    def pick(self) -> dict[str, str] | None:
        path = choose_pdf()
        if path is None:
            return None
        validate_pdf(path)
        token = uuid4().hex
        now = monotonic()
        with self._lock:
            self._selected = {key: value for key, value in self._selected.items() if value[1] > now}
            self._selected[token] = (path, now + 86400)
        return {"token": token, "name": path.name}

    def resolve(self, token: str) -> Path:
        with self._lock:
            selected = self._selected.get(token)
        if selected is None or selected[1] <= monotonic():
            raise ManualError("Choose the PDF again.")
        path = selected[0]
        validate_pdf(path)
        return path

    def clear(self, token: str) -> None:
        with self._lock:
            self._selected.pop(token, None)



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
    """Save a manually entered vacancy together with its requested or provided materials."""

    def __init__(self, storage: Storage):
        self._storage = storage

    def create(self, title: str, url: str, description: str, company: str = "", *, cv: bool = True,
               letter: bool = True, pdf: Path | None = None, cover_letter: str | None = None) -> ManualVacancy:
        title, description, company = (title or "").strip(), (description or "").strip(), (company or "").strip()
        cover_letter = cover_letter if cover_letter and cover_letter.strip() else None
        if (cv and pdf) or (letter and cover_letter):
            raise ManualError("Choose generation or an existing item for each attachment.")
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
            repo = self._storage.applications
            application = repo.create(vacancy_id, now, "apply", ApplicationState.QUEUED)
            repo.update(application.id, with_cv=int(cv), with_letter=int(letter), provided_pdf=str(pdf) if pdf else None,
                        provided_letter=cover_letter)
            application = repo.get(application.id)
            assert application is not None
        return ManualVacancy(vacancy_id, application)
