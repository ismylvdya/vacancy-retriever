from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ..config import CategoryRule, Rules
from .facts import VacancyFacts

Decision = Literal["show", "skip", "hidden"]
ResidencePermit = Literal["yes", "likely", "no"]

IT_RUSSIAN_EMPLOYERS = {"russian", "ru_with_serbian_entity", "intl_russian_speaking"}


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    skip_reason: str | None
    group: int | None
    stars: int | None
    residence_permit: ResidencePermit
    residence_reason: str
    english_flag: bool
    informal_flag: bool


class ScoringEngine:
    """Turns extracted facts into group / stars / residence permit / skip, using rules.yaml."""

    def __init__(self, rules: Rules):
        self._rules = rules

    def evaluate(
        self, facts: VacancyFacts, published_at: datetime | None, now: datetime
    ) -> Verdict:
        category = self._category(facts)
        in_serbia = self._works_in_serbia(facts)
        permit, permit_reason = self._residence_permit(facts, in_serbia)
        skip = self._skip_reason(facts, category, in_serbia, published_at, now)
        if category.kind == "hidden":
            return Verdict("hidden", "Not a vacancy", None, None, permit, permit_reason, False, False)
        group = self._group(facts, category, in_serbia)
        stars = self._stars(facts, category)
        return Verdict(
            decision="skip" if skip else "show",
            skip_reason=skip,
            group=group,
            stars=stars,
            residence_permit=permit,
            residence_reason=permit_reason,
            english_flag=facts.english_working,
            informal_flag=group == 4,
        )

    def _category(self, facts: VacancyFacts) -> CategoryRule:
        categories = self._rules.categories
        return categories.get(facts.role_category, categories["other"])

    def _works_in_serbia(self, facts: VacancyFacts) -> bool:
        country = facts.country.strip().lower()
        return country in self._rules.serbia_country_names or facts.serbian_entity in {"yes", "likely"}

    def _skip_reason(
        self,
        facts: VacancyFacts,
        category: CategoryRule,
        in_serbia: bool,
        published_at: datetime | None,
        now: datetime,
    ) -> str | None:
        if category.kind == "hidden":
            return "Not a vacancy"
        if category.kind == "drop":
            return category.drop_reason or category.label
        if facts.seniority in self._rules.dropped_seniority and facts.seniority_source in {"title", "label"}:
            return f"Grade: {facts.seniority}"
        russian_required = "ru" in facts.required_languages
        if facts.ad_language == "sr" and not russian_required:
            return "Ad in Serbian, Russian not required"
        if facts.serbian_required and not russian_required:
            return "Serbian required, Russian not required"
        if (
            facts.employer_type == "intl_english"
            and facts.english_working
            and facts.russian_speaking_team != "yes"
            and not in_serbia
        ):
            return "English-speaking company outside Serbia"
        if self._rules.drop_office_outside_serbia and self._office_abroad(facts, in_serbia):
            return "Office outside Serbia"
        if published_at and now - published_at > timedelta(days=self._rules.max_age_days):
            return "Expired"
        return None

    def _office_abroad(self, facts: VacancyFacts, in_serbia: bool) -> bool:
        country = facts.country.strip().lower()
        known_country = country not in {"", "unknown", "n/a", "remote"}
        return facts.work_format in {"office", "hybrid"} and known_country and not in_serbia

    def _group(self, facts: VacancyFacts, category: CategoryRule, in_serbia: bool) -> int:
        if facts.confidence == "low":
            return 5
        is_it = category.kind == "it"
        is_manual = category.kind == "manual"
        if in_serbia and facts.employment in {"official", "unknown"} and facts.employer_type != "private_person":
            if is_it:
                return 1
            if is_manual:
                return 2
        if is_manual and in_serbia and (
            facts.employment == "informal" or facts.employer_type == "private_person"
        ):
            return 4
        if (
            is_it
            and facts.work_format in {"remote", "unknown"}
            and (facts.employer_type in IT_RUSSIAN_EMPLOYERS or facts.russian_speaking_team == "yes")
        ):
            return 3
        return 5

    def _stars(self, facts: VacancyFacts, category: CategoryRule) -> int:
        penalties = self._rules.penalties
        minus = 0
        experienced = (
            facts.experience_years is not None
            and facts.experience_years >= penalties.experience_years_threshold
        )
        if category.kind == "it":
            if facts.stack_overlap == "partial":
                minus += penalties.stack_partial
            elif facts.stack_overlap == "none":
                minus += penalties.stack_none
            if facts.english_working:
                minus += penalties.english_working
        if category.kind in {"it", "manual"} and experienced:
            minus += penalties.experience
        return max(1, min(category.base - minus, category.cap))

    @staticmethod
    def _residence_permit(facts: VacancyFacts, in_serbia: bool) -> tuple[ResidencePermit, str]:
        reason = facts.serbian_entity_evidence
        if in_serbia and facts.visa_support == "yes":
            return "yes", reason or "residence permit support promised"
        if in_serbia and facts.serbian_entity == "yes" and facts.employment == "official":
            return "yes", reason
        if in_serbia and facts.employment in {"informal"}:
            return "no", reason or "informal"
        if facts.serbian_entity == "likely" or (in_serbia and facts.employment == "unknown"):
            return "likely", reason
        if in_serbia and facts.employment == "official":
            return "likely", reason
        return "no", reason
