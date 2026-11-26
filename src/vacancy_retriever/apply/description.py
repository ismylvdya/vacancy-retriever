"""The full description of a vacancy known only from a job-alert letter, looked for when the user applies.

For HH, try the public copy by vacancy ID first. Google for Jobs is the existing fallback.
When neither source has a description, the letter's text stays.
"""

from __future__ import annotations

import logging

from ..collectors.hh_descriptions import MIRROR_HOST, HhDescriptions, hh_vacancy_id
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
        text = self._from_hh_copy(raw) or self._from_job_search(raw)
        if not text:
            log.info("No description found for letter item %s", raw.id)
            return None
        self._storage.items.set_detail(raw.id, text)
        return text

    def _from_hh_copy(self, raw: StoredRawItem) -> str | None:
        if not hh_vacancy_id(raw.url) or MIRROR_HOST not in self._config.sources.detail_domains:
            return None
        lookup = HhDescriptions()
        try:
            return lookup.fetch(raw.url, raw.text)
        finally:
            lookup.close()

    def _from_job_search(self, raw: StoredRawItem) -> str | None:
        lookup = create_lookup(self._config)
        if lookup is None or not lookup.available():
            return None
        try:
            job_id = next(iter(linkedin_ids([raw.url or ""])), None)
            return lookup.lookup(raw.title or "", raw.meta.get("company") or "", raw.meta.get("location") or "", job_id)
        finally:
            lookup.close()
