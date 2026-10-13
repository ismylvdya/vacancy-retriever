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


@dataclass
class RawDeduplicator:
    """Finds reposts of an already collected item: same link, same text, or near-identical text elsewhere."""

    repo: RawItemRepository
    history_days: int = 30
    _candidates: list[_Candidate] = field(default_factory=list)
    _loaded: bool = False

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


@dataclass(frozen=True)
class DedupDecision:
    kind: str  # exact | gray | none
    other_id: int | None = None


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
