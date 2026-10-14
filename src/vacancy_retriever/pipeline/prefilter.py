from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import Rules
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
        self._deny = _word_pattern(prefilter.title_deny_keywords)
        self._grades = _word_pattern(prefilter.title_grade_keywords)
        self._junior = _word_pattern(prefilter.title_junior_markers)

    def check(self, item: StoredRawItem, now: datetime) -> PrefilterResult:
        if item.published_at and now - item.published_at > timedelta(days=self._rules.max_age_days):
            return PrefilterResult("skip", "Expired")
        if item.meta.get("rvc"):
            return self._check_rvc(item)
        if item.multi:
            return KEEP
        return self._check_title(item.title)

    def _check_rvc(self, item: StoredRawItem) -> PrefilterResult:
        emoji = item.meta.get("grade_emoji", "")
        is_ad = any(marker.lower() in item.text.lower() for marker in self._rules.prefilter.rvc_ad_markers)
        if emoji not in RVC_VACANCY_EMOJI or (is_ad and not item.meta.get("grade")):
            return PrefilterResult("hide", "Advertisement")
        grade = item.meta.get("grade") or RVC_GRADE_EMOJI.get(emoji, "")
        if grade in self._rules.prefilter.rvc_dropped_grades:
            return PrefilterResult("skip", f"RVC grade: {grade}")
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

    def _check_title(self, title: str) -> PrefilterResult:
        if not title:
            return KEEP
        lowered = title.lower()
        reason = self._rules.prefilter.title_deny_reason
        match = self._deny.search(lowered) if self._deny else None
        if match:
            return PrefilterResult("skip", f"{reason}: {match.group(1)}")
        if self._junior and self._junior.search(lowered):
            return KEEP
        match = self._grades.search(lowered) if self._grades else None
        if match:
            return PrefilterResult("skip", f"{reason}: {match.group(1)}")
        return KEEP


def _word_pattern(words: list[str]) -> re.Pattern[str] | None:
    if not words:
        return None
    ordered = sorted(words, key=len, reverse=True)
    return re.compile(r"(?<![\w+#])(" + "|".join(re.escape(w.lower()) for w in ordered) + r")(?![\w+#])", re.I)
