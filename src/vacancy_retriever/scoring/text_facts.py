"""Facts found by code in the text of one vacancy, without the LLM: required years and any link to Russian.

The LLM reads only the first 4000 characters and sometimes misses the years; `recalculate` needs no LLM call at all.
A post with several vacancies (multi) is left alone: the text does not say which vacancy a phrase belongs to.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Rules
from .facts import VacancyFacts

# Years count only next to the word «experience» / «опыт» / «iskustvo»: "For over 25 years we…" and "your data is kept
# for 2 years" are not requirements. "A minimum of 4 years in software" is the one form allowed without the word.
_RANGE = r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to|до)\s*\d{1,2}\s*\+?\s*)?"
_EXPERIENCE = re.compile("|".join([
    _RANGE + r"(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+|in\s+|with\s+)?(?:[\w/&.-]+\s+){0,4}?experience",
    r"(?:at\s+least|minimum(?:\s+of)?)\s+" + _RANGE + r"(?:years?|yrs?)",
    r"experience\s*(?:of\s*)?[:\-–]?\s*" + _RANGE + r"(?:years?|yrs?)",
    r"опыт\w*(?:\s+[\w/&.-]+){0,6}?\s+(?:от\s+)?" + _RANGE + r"(?:лет|года|год)(?!\w)",
    r"(?:от\s+)?" + _RANGE + r"(?:лет|года|год)\s+(?:\w+\s+)?опыт",
    r"iskustv\w*(?:\s+\w+){0,4}?\s+(?:od\s+)?" + _RANGE + r"godin",
    r"(?:od\s+)?" + _RANGE + r"godin\w*\s+(?:\w+\s+){0,2}iskustv",
]), re.I)
MAX_YEARS = 15  # more than this is the age of the company, not a requirement


def word_pattern(words: list[str]) -> re.Pattern[str] | None:
    """Whole words, any case; a trailing * allows any ending: «директор*» also finds «директора»."""
    if not words:
        return None
    parts = [re.escape(w[:-1].lower()) + r"\w*" if w.endswith("*") else re.escape(w.lower())
             for w in sorted(words, key=len, reverse=True)]
    return re.compile(r"(?<![\w+#])(" + "|".join(parts) + r")(?![\w+#])", re.I)


def experience_from_text(text: str) -> float | None:
    """The smallest number of years the text asks for: a vacancy «1–2 years … 5 years of Python» is open from 1."""
    years = [int(value) for match in _EXPERIENCE.finditer(text) for value in match.groups() if value]
    years = [value for value in years if value <= MAX_YEARS]
    return float(min(years)) if years else None


def fill_from_text(facts: VacancyFacts, rules: Rules, item: Any) -> VacancyFacts:
    """Adds to the facts of a single-vacancy item what its stored text says. Never replaces the LLM's years."""
    if item.multi:
        return facts
    text = f"{item.text}\n{item.detail_text or ''}"
    if facts.experience_years is None:
        facts.experience_years = experience_from_text(text)
    pattern = word_pattern(rules.russian_link_words)
    facts.russian_mention = bool(pattern and pattern.search(text))
    return facts
