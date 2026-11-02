"""The full description of a vacancy known only from a job-alert letter, looked for when the user applies.

Letters carry one line («Product Analyst Playrix Сербия hh.ru — автопоиск»), and the note, the CV and the cover letter
need the real text. Only Google for Jobs is asked (collectors/job_search.py, the API hosts and nothing else): LinkedIn
is never opened, and hh.ru refuses programs. Nothing found — the letter's text stays, as before.
"""

from __future__ import annotations

import logging

from ..collectors.job_search import create_lookup, linkedin_ids
from ..config import AppConfig
from ..storage import Storage
from ..storage.items import StoredRawItem

log = logging.getLogger(__name__)


class LetterDescriptions:
    def __init__(self, config: AppConfig, storage: Storage):
        self._config = config
        self._storage = storage
        self._tried: set[int] = set()  # one try per vacancy while the app runs: a miss costs API requests

    def complete(self, raw: StoredRawItem) -> str | None:
        """The description now stored for this letter-only item, or None when none was found."""
        if not raw.email_only or raw.id in self._tried:
            return None
        self._tried.add(raw.id)
        lookup = create_lookup(self._config)
        if lookup is None or not lookup.available():
            return None
        try:
            job_id = next(iter(linkedin_ids([raw.url or ""])), None)
            text = lookup.lookup(raw.title or "", raw.meta.get("company") or "", raw.meta.get("location") or "", job_id)
        finally:
            lookup.close()
        if not text:
            log.info("No description found for letter item %s", raw.id)
            return None
        self._storage.items.set_detail(raw.id, text)
        return text
