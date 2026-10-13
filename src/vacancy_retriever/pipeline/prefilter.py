from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import Rules
from ..storage.items import StoredRawItem

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
        words = sorted(rules.prefilter.title_deny_keywords, key=len, reverse=True)
        self._deny = (
            re.compile(r"(?<![\w+#])(" + "|".join(re.escape(w.lower()) for w in words) + r")(?![\w+#])", re.I)
            if words
            else None
        )

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
        return self._check_title(item.title)

    def _check_title(self, title: str) -> PrefilterResult:
        if not title or not self._deny:
            return KEEP
        match = self._deny.search(title.lower())
        if match:
            return PrefilterResult("skip", f"{self._rules.prefilter.title_deny_reason}: {match.group(1)}")
        return KEEP
