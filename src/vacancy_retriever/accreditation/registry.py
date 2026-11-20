"""The register of accredited IT organisations, as a local file.

Since October 2022 the Ministry publishes no full register (only a one-company search on Gosuslugi). A full snapshot
with INN and status (active / annulled) is kept on GitHub by a third party: github.com/DverkaSK/gosuslugi-itorgs
(files data/YYYY-MM-DD.json; checked 20.11.2026: snapshot of 03.11.2026, 40 282 records, 19 104 active).
The newest snapshot is downloaded into data/it_registry/ and looked for again every `refresh_days`.
A file put there by hand (same format) is used too: the newest date in the name wins.
"""

from __future__ import annotations

import bisect
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..collectors.http import PoliteHttpClient
from ..timeutil import from_iso, to_iso, utcnow
from .names import compact, inn_digits, normalize, short_name

log = logging.getLogger(__name__)

SNAPSHOT_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
ACTIVE = "Действует"


@dataclass(frozen=True)
class RegistryRecord:
    inn: str
    ogrn: str
    name: str
    status: str
    change_date: str = ""
    change_description: str = ""

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    def label(self) -> str:
        state = "в реестре" if self.active else f"аккредитация аннулирована{(' ' + self.change_date) if self.change_date else ''}"
        return f"{short_name(self.name)} (ИНН {self.inn}) — {state}"


class Registry:
    """Index of one snapshot: by INN, by normalised name and by name-word prefix (to find a brand's companies)."""

    def __init__(self, records: list[RegistryRecord], snapshot_date: str):
        self.snapshot_date = snapshot_date
        self.records = records
        self.by_inn: dict[str, RegistryRecord] = {}
        self.by_name: dict[str, list[RegistryRecord]] = {}
        words: dict[str, set[int]] = {}
        for index, record in enumerate(records):
            # One INN can have an old annulled record and a new active one: the active one wins.
            previous = self.by_inn.get(record.inn)
            if previous is None or (record.active and not previous.active):
                self.by_inn[record.inn] = record
            self.by_name.setdefault(compact(record.name), []).append(record)
            for word in {*normalize(record.name).split(), compact(record.name)}:
                if len(word) >= 3:
                    words.setdefault(word, set()).add(index)
        self._words = sorted(words)
        self._word_records = words

    @classmethod
    def load(cls, path: Path) -> Registry:
        data = json.loads(path.read_text(encoding="utf-8"))
        records = [
            RegistryRecord(inn_digits(r.get("inn")), str(r.get("ogrn") or ""), str(r.get("name") or ""),
                           str(r.get("status") or ""), str(r.get("change_date") or ""),
                           str(r.get("change_description") or ""))
            for r in data.get("records") or [] if r.get("inn")
        ]
        snapshot = str(data.get("snapshot_date") or (SNAPSHOT_NAME.match(path.name) or [None, ""])[1])
        return cls(records, snapshot)

    def find_inn(self, inn: str) -> RegistryRecord | None:
        return self.by_inn.get(inn_digits(inn))

    def find_name(self, name: str) -> list[RegistryRecord]:
        return list(self.by_name.get(compact(name), []))

    def with_prefix(self, prefix: str, limit: int = 15) -> list[RegistryRecord]:
        """Records with a name word starting with the prefix («сбер» → «СберТех», «СберДевайсы»…), active first,
        short names first (the brand's own company before «Сберегательные технологии регионов»)."""
        prefix = prefix.lower().replace("ё", "е")
        if len(prefix) < 3:
            return []
        found: set[int] = set()
        start = bisect.bisect_left(self._words, prefix)
        for word in self._words[start:]:
            if not word.startswith(prefix):
                break
            found |= self._word_records[word]
            if len(found) > 400:
                break
        chosen = sorted((self.records[i] for i in found), key=lambda r: (not r.active, len(normalize(r.name)), r.name))
        unique: dict[str, RegistryRecord] = {}
        for record in chosen:
            unique.setdefault(record.inn, record)
        return list(unique.values())[:limit]


class RegistryStore:
    """data/it_registry/: snapshots and state.json (when GitHub was last asked for a newer one)."""

    def __init__(self, folder: Path, listing_url: str, refresh_days: int):
        self.folder = folder
        self.listing_url = listing_url
        self.refresh_days = refresh_days
        self._cached: Registry | None = None
        self._cached_path: Path | None = None

    def newest_file(self) -> Path | None:
        files = sorted(p for p in self.folder.glob("*.json") if SNAPSHOT_NAME.match(p.name))
        return files[-1] if files else None

    def load(self) -> Registry | None:
        path = self.newest_file()
        if path is None:
            return None
        if self._cached is None or self._cached_path != path:
            self._cached, self._cached_path = Registry.load(path), path
        return self._cached

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def due(self, now: datetime | None = None) -> bool:
        checked = from_iso(self._state().get("checked_at"))
        return checked is None or (now or utcnow()) - checked >= timedelta(days=self.refresh_days)

    def refresh(self, http: PoliteHttpClient, now: datetime | None = None, force: bool = False) -> str | None:
        """Downloads a snapshot newer than the local one. Returns a problem to show, None = fine."""
        if not force and self.newest_file() is not None and not self.due(now):
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            listing = http.get_json(self.listing_url)
            names = sorted(e["name"] for e in listing if isinstance(e, dict) and SNAPSHOT_NAME.match(e.get("name", "")))
            if not names:
                return "no register snapshots in the listing"
            newest = names[-1]
            local = self.newest_file()
            if local is None or local.name < newest:
                entry = next(e for e in listing if e.get("name") == newest)
                data = http.get_bytes(entry["download_url"])
                json.loads(data)  # a broken download must not replace a good file
                (self.folder / f".{newest}.part").write_bytes(data)
                (self.folder / f".{newest}.part").replace(self.folder / newest)
                log.info("IT register: downloaded snapshot %s (%d bytes)", newest, len(data))
        except Exception as exc:  # noqa: BLE001 - an old snapshot is still good; the check is retried next run
            log.warning("IT register refresh failed: %s", exc)
            return f"IT register: {exc}"
        (self.folder / "state.json").write_text(
            json.dumps({"checked_at": to_iso(now or utcnow()), "newest": newest}, ensure_ascii=False), encoding="utf-8")
        return None

    def age_days(self, today: date | None = None) -> int | None:
        path = self.newest_file()
        if path is None:
            return None
        snapshot = date.fromisoformat(SNAPSHOT_NAME.match(path.name).group(1))
        return ((today or date.today()) - snapshot).days
