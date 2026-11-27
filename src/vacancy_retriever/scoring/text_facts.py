"""Facts found by code in the text of one vacancy, without the LLM: required years and grade, any link to Russian,
a sign of no IT accreditation.

The LLM reads only the first 4000 characters and sometimes misses the years; `recalculate` needs no LLM call at all.
A post with several vacancies (multi) is left alone: the text does not say which vacancy a phrase belongs to.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Rules
from .facts import VacancyFacts
from .requirements import MAX_YEARS, RequirementsResolver, grade_from_labels, years_from_labels

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


class TextFacts:
    """What code adds to the LLM's facts of one item: years and grade where the LLM found none, the deciding
    requirement (scoring/requirements.py), a link to Russian and a sign of no IT accreditation."""

    def __init__(self, rules: Rules):
        self._rules = rules
        self._russian = word_pattern(rules.russian_link_words)
        self._no_accreditation = word_pattern(rules.no_it_accreditation_signs.words)
        self._requirements = RequirementsResolver(rules)

    def fill(self, facts: VacancyFacts, item: Any) -> VacancyFacts:
        """Never replaces what the LLM found. A post with several vacancies (multi) keeps its text out of it: the
        text does not say which vacancy a phrase belongs to."""
        if not item.multi:
            self._fill_requirements(facts, item)
            facts.russian_mention = bool(self._russian and self._russian.search(f"{item.text}\n{item.detail_text or ''}"))
        self._requirements.resolve(facts)
        facts.no_accreditation_sign = self._accreditation_sign(facts, item)
        return facts

    def _fill_requirements(self, facts: VacancyFacts, item: Any) -> None:
        if not facts.requirements_by_place:  # scored before 27.11.2026: one value for the whole text, as then
            if facts.experience_years is None:
                facts.experience_years = experience_from_text(f"{item.text}\n{item.detail_text or ''}")
            return
        found = {"text": experience_from_text(item.description), "label": years_from_labels(item.page_meta)}
        for place, years in found.items():
            if years is not None:
                facts.experience_by.setdefault(place, years)
        label_grade = grade_from_labels(item.page_meta)
        if label_grade:
            facts.grade_by.setdefault("label", label_grade)  # type: ignore[arg-type]

    def _accreditation_sign(self, facts: VacancyFacts, item: Any) -> str:
        signs = self._rules.no_it_accreditation_signs
        if facts.employer_type in signs.employer_types:
            return f"Employer is a {facts.employer_type.replace('_', ' ')}"
        if signs.placeholder_employer and facts.employer_placeholder:
            return f"«{facts.employer_name}» is not a company name"
        texts = [facts.employer_name, *item.page_meta] + ([] if item.multi else [item.description])
        match = self._no_accreditation.search("\n".join(texts)) if self._no_accreditation else None
        return f"The vacancy mentions «{match.group(1)}»" if match else ""


def fill_from_text(facts: VacancyFacts, rules: Rules, item: Any) -> VacancyFacts:
    return TextFacts(rules).fill(facts, item)
