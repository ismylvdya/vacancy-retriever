from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from ..collectors.base import RawItem, normalize_text, normalize_url
from ..storage.items import RawItemRepository

FUZZY_MIN_LENGTH = 200
FUZZY_THRESHOLD = 90
LENGTH_TOLERANCE = 0.25


@dataclass
class _Candidate:
    raw_id: int
    source_id: str
    length: int
    text: str


TITLE_MIN_LENGTH = 12


def _first_line(text: str) -> str:
    return next((line for line in (text or "").splitlines() if line.strip()), "")


@dataclass
class TitleMatch:
    raw_id: int
    # The stored item is a channel post of this run still waiting for the LLM, the new one is a site / e-mail
    # card of the same vacancy: the card (richer text) becomes the original and the post its duplicate.
    swap: bool = False


@dataclass
class _Titled:
    raw_id: int
    source_id: str
    title: str        # normalized: the item title, or the first line of a channel post
    company: str      # normalized company of a card ("" for posts)
    head: str         # normalized start of the text (posts: to find the company in it)
    multi: bool
    pending_here: bool


@dataclass
class RawDeduplicator:
    """Finds reposts of an already collected item: same link, same text, or near-identical text elsewhere."""

    repo: RawItemRepository
    history_days: int = 30
    run_id: int | None = None
    _candidates: list[_Candidate] = field(default_factory=list)
    _loaded: bool = False
    _titled: list[_Titled] = field(default_factory=list)
    _titles_loaded: bool = False

    def _load(self) -> None:
        if self._loaded:
            return
        for raw_id, source_id, text in self.repo.recent_texts(self.history_days, FUZZY_MIN_LENGTH):
            normalized = normalize_text(text)
            self._candidates.append(_Candidate(raw_id, source_id, len(normalized), normalized))
        self._loaded = True

    def find_original(self, item: RawItem) -> int | None:
        if item.url:
            found = self.repo.find_by_norm_url(normalize_url(item.url), exclude_source=item.source_id)
            if found:
                return found
        found = self.repo.find_by_hash(item.text_hash)
        if found:
            return found
        return self._fuzzy(item)

    def remember(self, raw_id: int, item: RawItem) -> None:
        normalized = normalize_text(item.text)
        if len(normalized) > FUZZY_MIN_LENGTH:
            self._candidates.append(_Candidate(raw_id, item.source_id, len(normalized), normalized))
        if self._titles_loaded:
            titled = self._as_titled(raw_id, item.source_id, item.title, item.text, item.meta.get("company", ""),
                                     item.multi, True)
            if titled:
                self._titled.append(titled)

    # ---- exact title dedup (before the LLM, free): the same vacancy from two sources is evaluated once
    @staticmethod
    def _as_titled(raw_id: int, source_id: str, title: str, text: str, company: str, multi: bool,
                   pending_here: bool) -> _Titled | None:
        name = normalize_text(_first_line(text) if multi else title)
        if len(name) < TITLE_MIN_LENGTH:
            return None
        # The head is normalized like a company name (legal forms dropped), so "TRUSTONE ONE DOO BEOGRAD" in a post
        # matches the card's company "TRUSTONE ONE DOO BEOGRAD" → "trustone one beograd".
        return _Titled(raw_id, source_id, name, normalize_company(company or ""), normalize_company(text[:400]),
                       multi, pending_here)

    def _load_titles(self) -> None:
        if self._titles_loaded:
            return
        for row in self.repo.recent_titled(self.history_days):
            titled = self._as_titled(row["id"], row["source_id"], row["title"] or "", row["head"] or "",
                                     row["company"] or "", bool(row["multi"]),
                                     row["status"] == "pending_llm" and row["run_id"] == self.run_id)
            if titled:
                self._titled.append(titled)
        self._titles_loaded = True

    def find_same_title(self, item: RawItem) -> TitleMatch | None:
        """Same vacancy in another source: a card with the same title and company, or a channel post whose first line
        is the card's title and whose text names the card's company. Similar-but-different titles are NOT merged —
        a rare duplicate is better than a wrong merge (decision 2026-10-26)."""
        new = self._as_titled(0, item.source_id, item.title, item.text, item.meta.get("company", ""), item.multi, True)
        if new is None:
            return None
        self._load_titles()
        for old in self._titled:
            if old.source_id == item.source_id or old.title != new.title:
                continue
            if not new.multi and not old.multi and new.company and new.company == old.company:
                return TitleMatch(old.raw_id)
            if not new.multi and old.multi and new.company and new.company in old.head:
                return TitleMatch(old.raw_id, swap=old.pending_here)
            if new.multi and not old.multi and old.company and old.company in new.head:
                return TitleMatch(old.raw_id)
        return None

    def _fuzzy(self, item: RawItem) -> int | None:
        normalized = normalize_text(item.text)
        if len(normalized) <= FUZZY_MIN_LENGTH:
            return None
        self._load()
        low, high = len(normalized) * (1 - LENGTH_TOLERANCE), len(normalized) * (1 + LENGTH_TOLERANCE)
        pool = [c for c in self._candidates if c.source_id != item.source_id and low <= c.length <= high]
        if not pool:
            return None
        match = process.extractOne(
            normalized, [c.text for c in pool], scorer=fuzz.token_set_ratio, score_cutoff=FUZZY_THRESHOLD
        )
        return pool[match[2]].raw_id if match else None


_LEGAL = re.compile(r"\b(d\.?o\.?o\.?|ooo|ооо|llc|ltd|inc|gmbh|ао|зао|оао|ип|corp|company|компания)\b", re.I)
_GRADE = re.compile(r"\b(junior|middle|senior|intern|trainee|стажер|стажёр|младший|джуниор)\b", re.I)


def normalize_company(name: str) -> str:
    name = _LEGAL.sub(" ", (name or "").lower().replace("«", " ").replace("»", " "))
    name = normalize_text(name)
    return "" if name in {"unknown", "не указано", "n a", "none"} else name


def normalize_title(title: str) -> str:
    return normalize_text(_GRADE.sub(" ", title or ""))


def dedup_key(employer: str, title: str) -> str | None:
    company = normalize_company(employer)
    if not company:
        return None
    return f"{company}|{normalize_title(title)}"


def compare_vacancies(employer_a: str, title_a: str, employer_b: str, title_b: str) -> str:
    """exact (same normalized company+title) | gray (similar, needs LLM) | none."""
    company_a, company_b = normalize_company(employer_a), normalize_company(employer_b)
    title_a_n, title_b_n = normalize_title(title_a), normalize_title(title_b)
    if not title_a_n or not title_b_n:
        return "none"
    if company_a and company_a == company_b and title_a_n == title_b_n:
        return "exact"
    title_score = fuzz.token_set_ratio(title_a_n, title_b_n)
    if company_a and company_b:
        if fuzz.ratio(company_a, company_b) >= 85 and title_score >= 75:
            return "gray"
        return "none"
    if title_score >= 95 and len(title_a_n) > 12:
        return "gray"
    return "none"
