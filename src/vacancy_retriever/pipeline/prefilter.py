from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import Rules
from ..scoring.requirements import grade_from_labels, years_from_labels
from ..scoring.text_facts import experience_from_text, word_pattern
from ..storage.items import StoredRawItem

_LATIN_WORD = re.compile(r"[a-zčćšžđ]+")
_SERBIAN_WORDS = frozenset(
    "je i u za na sa da se od ili kao koji koja koje rad rada radno radnog mesto mesta posao posla "
    "iskustvo iskustva poznavanje znanje kandidat kandidata kandidati nudimo uslovi potrebno potrebna "
    "zahtevi obaveze odgovornosti prijava prijave jezika ćete će smo naš naša vaš vaša".split()
)
_ENGLISH_WORDS = frozenset(
    "the and of to with for you we our your experience will are is in a team skills knowledge "
    "working work role responsibilities requirements".split()
)
_SERBIAN_CYRILLIC = re.compile(r"[ђјљњћџЂЈЉЊЋЏ]")
_RUSSIAN_MENTION = re.compile(r"rusk|русск|руск|russian", re.I)


def looks_serbian(text: str) -> bool:
    """Serbian Latin (word statistics) or Serbian Cyrillic (letters Russian does not have)."""
    if _SERBIAN_CYRILLIC.search(text):
        return True
    words = _LATIN_WORD.findall(text.lower())
    serbian = sum(word in _SERBIAN_WORDS for word in words)
    english = sum(word in _ENGLISH_WORDS for word in words)
    return serbian >= 5 and serbian > english * 1.2


def mentions_russian(text: str) -> bool:
    return bool(_RUSSIAN_MENTION.search(text))


RVC_GRADE_EMOJI = {"🟩": "junior", "🟨": "middle", "🟥": "senior"}
RVC_VACANCY_EMOJI = set("🟩🟨🟥🟪⬜")


@dataclass(frozen=True)
class PrefilterResult:
    action: str  # keep | skip | hide
    reason: str | None = None


KEEP = PrefilterResult("keep")


class Prefilter:
    """Cheap rules before the LLM. Keyword rules apply only to single-vacancy items."""

    def __init__(self, rules: Rules):
        self._rules = rules
        prefilter = rules.prefilter
        self._deny = word_pattern(prefilter.title_deny_keywords)
        self._grades = word_pattern(prefilter.title_grade_keywords)
        self._junior = word_pattern(prefilter.title_junior_markers)
        self._programme = word_pattern(prefilter.title_programme_markers)

    def check(self, item: StoredRawItem, now: datetime) -> PrefilterResult:
        if item.meta.get("ignore"):
            return PrefilterResult("hide", item.meta["ignore"])
        if item.published_at and now - item.published_at > timedelta(days=self._rules.max_age_days):
            return PrefilterResult("skip", "Expired")
        if item.meta.get("rvc"):
            return self._check_rvc(item)
        if item.multi:
            return KEEP
        return self._check_title(item.title)

    def check_requirements(self, item: StoredRawItem) -> PrefilterResult:
        """After details are fetched, for free: the years code finds by the priority of places (rules.yaml →
        requirements) decide; the grade drops only a vacancy that states no years anywhere."""
        if item.multi or self._is_programme(item.title):
            return KEEP
        years = self._years(item)
        if years is not None and self._rules.requirements.years_over_grade:
            limit = self._years_limit(item)
            return PrefilterResult("skip", f"Experience: {years:g}+ years") if limit is not None and years >= limit else KEEP
        grade = str(item.meta.get("grade") or "").strip().lower()
        if item.meta.get("rvc"):
            grade = grade or RVC_GRADE_EMOJI.get(item.meta.get("grade_emoji", ""), "")
            if grade in self._rules.prefilter.rvc_dropped_grades:
                return PrefilterResult("skip", f"RVC grade: {grade}")
        elif grade in self._rules.dropped_seniority:  # sites that state the grade in their data (Habr «qualification»)
            return PrefilterResult("skip", f"Grade: {grade}")
        return self._check_title_grade(item.title)

    def _years(self, item: StoredRawItem) -> float | None:
        finders = {"text": lambda: experience_from_text(item.description),
                   "title": lambda: experience_from_text(item.title),
                   "label": lambda: years_from_labels(item.page_meta)}
        for place in self._rules.requirements.priority:
            years = finders[place]()
            if years is not None:
                return years
        return None

    def _years_limit(self, item: StoredRawItem) -> float | None:
        limits = self._rules.experience_drop
        grade = str(item.meta.get("grade") or "").strip().lower() or grade_from_labels(item.page_meta)
        junior = grade in {"intern", "junior"} or bool(self._junior and self._junior.search(item.title.lower()))
        return limits.years if junior or limits.years_unless_junior is None else limits.years_unless_junior

    def _check_rvc(self, item: StoredRawItem) -> PrefilterResult:
        emoji = item.meta.get("grade_emoji", "")
        is_ad = any(marker.lower() in item.text.lower() for marker in self._rules.prefilter.rvc_ad_markers)
        if emoji not in RVC_VACANCY_EMOJI or (is_ad and not item.meta.get("grade")):
            return PrefilterResult("hide", "Advertisement")
        if self._rvc_foreign(item):
            return PrefilterResult("skip", "RVC: no Russian, not Serbia")
        return self._check_title(item.title)

    def check_language(self, item: StoredRawItem, text: str) -> PrefilterResult:
        """After details are fetched, for sources with `serbian_needs_russian: true`: an ad in Serbian
        that never mentions Russian is dropped by the same rule the LLM stage would apply — for free."""
        if looks_serbian(text) and not mentions_russian(text):
            return PrefilterResult("skip", "Ad in Serbian, Russian not required")
        return KEEP

    def _rvc_foreign(self, item: StoredRawItem) -> bool:
        """Only when the post lists required languages: no Russian there and no Serbia mention."""
        prefilter = self._rules.prefilter
        languages = item.meta.get("required_languages")
        if not prefilter.rvc_require_russian_or_serbia or not languages or "ru" in languages:
            return False
        lowered = item.text.lower()
        return not any(marker.lower() in lowered for marker in prefilter.rvc_serbia_markers)

    def _is_programme(self, title: str) -> bool:
        """A programme / wave announcement: the LLM decides, keywords never drop it."""
        return bool(title and self._programme and self._programme.search(title.lower()))

    def _check_title(self, title: str) -> PrefilterResult:
        """Roles that never fit. Grade words of the title wait for the description (check_requirements)."""
        if not title or self._is_programme(title):
            return KEEP
        match = self._deny.search(title.lower()) if self._deny else None
        if match:
            return PrefilterResult("skip", f"{self._rules.prefilter.title_deny_reason}: {match.group(1)}")
        return KEEP

    def _check_title_grade(self, title: str) -> PrefilterResult:
        lowered = title.lower()
        if not title or (self._junior and self._junior.search(lowered)):
            return KEEP
        match = self._grades.search(lowered) if self._grades else None
        if match:
            return PrefilterResult("skip", f"{self._rules.prefilter.title_deny_reason}: {match.group(1)}")
        return KEEP


