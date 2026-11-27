"""Required experience and grade of a vacancy: where each was found and which place decides.

A vacancy can state them in three places (rules.yaml → requirements.priority):
  text  — the description;
  title — the vacancy's title;
  label — the data the page shows above the description: T-Bank's tags «Middle, Senior, 1 – 5 лет», hh's
          «Опыт работы: От 1 года до 3 лет», Habr's grade (`meta["page_meta"]`, written by the collectors).
The LLM (and, where it is silent, code) finds them per place; the first place of the priority that has a value wins.
Inside one place several values give the smallest: «1–3 года» is 1, «Middle, Senior» is middle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..config import Rules
from .facts import GRADE_ORDER, PLACES, VacancyFacts

MAX_YEARS = 15  # more than this is the age of the company, not a requirement

_NO_EXPERIENCE = re.compile(r"без\s+опыта|нет\s+опыта|опыт\s+не\s+(?:нужен|требуется|обязателен)|не\s+требуется"
                            r"|no\s+experience|bez\s+iskustva", re.I)
_UNIT = r"(?:лет|года|год|years?|yrs?|godin\w*)"
_LABEL_YEARS = [
    re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|—|до|to)\s*\d{1,2}\s*\+?\s*" + _UNIT, re.I),
    re.compile(r"(?:от|from|od)\s*(\d{1,2})\s*" + _UNIT, re.I),
    re.compile(r"(?:более|больше|свыше|over|more\s+than|više\s+od)\s*(\d{1,2})", re.I),
    re.compile(r"(\d{1,2})\s*\+?\s*" + _UNIT, re.I),
    re.compile(r"(?:опыт|experience|iskustvo)[^:\n]*:\s*(\d{1,2}(?:[.,]\d)?)(?!\d)", re.I),
]
_GRADE_WORDS = {
    "intern": ["intern", "internship", "trainee", "стажер", "стажёр", "стажировка", "praksa", "praktikant"],
    "junior": ["junior", "джуниор", "джун", "младший"],
    "middle": ["middle", "middle+", "мидл"],
    "senior": ["senior", "сеньор", "сеньер", "старший"],
    "lead": ["lead", "teamlead", "team lead", "тимлид", "head", "director", "c-level"],
}
_GRADE_PATTERNS = {grade: re.compile(r"(?<![\w+])(" + "|".join(map(re.escape, words)) + r")(?![\w+])", re.I)
                   for grade, words in _GRADE_WORDS.items()}


def lowest_grade(grades: Iterable[str]) -> str | None:
    known = [g for g in grades if g in GRADE_ORDER]
    return min(known, key=GRADE_ORDER.index) if known else None


def years_from_labels(lines: Iterable[str]) -> float | None:
    """Years in the page's own data: «От 1 года до 3 лет» → 1, «1 – 5 лет» / «Более 5 лет» → 1, «Нет опыта» → 0,
    «Опыт (лет): 2» → 2, RVC «| 1 year(s) |» → 1."""
    found: list[float] = []
    for line in lines:
        if _NO_EXPERIENCE.search(line):
            found.append(0.0)
        for pattern in _LABEL_YEARS:
            found.extend(float(m.group(1).replace(",", ".")) for m in pattern.finditer(line))
    found = [value for value in found if value <= MAX_YEARS]
    return min(found) if found else None


def grade_from_labels(lines: Iterable[str]) -> str | None:
    """The lowest grade the page's data names: T-Bank «Middle, Senior» → middle."""
    text = "\n".join(lines)
    return lowest_grade(grade for grade, pattern in _GRADE_PATTERNS.items() if pattern.search(text))


class RequirementsResolver:
    """Sets the deciding `experience_years` / `seniority` (+ their source) from the values found per place."""

    def __init__(self, rules: Rules):
        self._priority = [place for place in rules.requirements.priority if place in PLACES]

    def resolve(self, facts: VacancyFacts) -> VacancyFacts:
        if not facts.requirements_by_place:
            return facts  # scored before 27.11.2026: one value for the whole vacancy, kept as it was
        years_place = next((p for p in self._priority if facts.experience_by.get(p) is not None), None)
        facts.experience_years = facts.experience_by[years_place] if years_place else None
        facts.experience_source = years_place or "none"  # type: ignore[assignment]
        grade_place = next((p for p in self._priority if facts.grade_by.get(p)), None)
        facts.seniority = facts.grade_by[grade_place] if grade_place else "unknown"  # type: ignore[assignment]
        facts.seniority_source = grade_place or "none"  # type: ignore[assignment]
        return facts
