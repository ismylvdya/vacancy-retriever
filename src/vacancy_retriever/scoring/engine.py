from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ..config import CategoryRule, Rules
from .facts import RUSSIAN_EMPLOYERS, VacancyFacts
from .salary import format_rub, monthly_rub, parse_salary
from .text_facts import word_pattern

Decision = Literal["show", "skip", "hidden"]
ResidencePermit = Literal["yes", "likely", "no"]


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
        self._title_grades = word_pattern(rules.prefilter.title_grade_keywords)
        self._title_juniors = word_pattern(rules.prefilter.title_junior_markers)

    def evaluate(
        self, facts: VacancyFacts, published_at: datetime | None, now: datetime,
        rates: dict[str, float] | None = None, email_only: bool = False,
    ) -> Verdict:
        """rates: RUB per currency unit on the day the vacancy was collected (None = no salary rule).
        email_only: all that is known comes from a job-alert letter, there is no description."""
        category = self._category(facts)
        in_serbia = self._works_in_serbia(facts)
        permit, permit_reason = self._residence_permit(facts, in_serbia)
        skip = self._skip_reason(facts, category, in_serbia, published_at, now) or self._salary_too_high(facts, rates)
        if category.kind == "hidden":
            return Verdict("hidden", "Not a vacancy", None, None, permit, permit_reason, False, False)
        group = self._group(facts, category, in_serbia)
        skip = skip or self._group_off(group)
        stars = self._stars(facts, category, email_only)
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

    def salary_floor_rub(self, facts: VacancyFacts, rates: dict[str, float] | None) -> float | None:
        salary = parse_salary(facts.salary_text)
        if salary is None or not rates:
            return None
        return monthly_rub(salary.low, salary, rates, self._rules.salary.hours_per_month)

    def _salary_too_high(self, facts: VacancyFacts, rates: dict[str, float] | None) -> str | None:
        limit = self._rules.salary.max_floor_rub
        if facts.role_category == "not_a_vacancy" or not limit:
            return None
        floor = self.salary_floor_rub(facts, rates)
        if floor is not None and floor > limit:
            return f"Salary floor above {format_rub(limit)}"
        return None

    def explain(self, facts: VacancyFacts, published_at: datetime | None, now: datetime,
                rates: dict[str, float] | None = None, email_only: bool = False) -> list[str]:
        """Debug mode: the same decision as evaluate(), step by step, in plain words."""
        category = self._category(facts)
        in_serbia = self._works_in_serbia(facts)
        country = facts.country or "unknown"
        lines = [f"Category: {facts.role_category} → {category.kind} (base {category.base}★, cap {category.cap}★)",
                 self._requirements_line(facts)]
        lines.append(f"Works in Serbia: {'yes' if in_serbia else 'no'} (country {country}, "
                     f"serbian_entity {facts.serbian_entity})")
        skip = self._skip_reason(facts, category, in_serbia, published_at, now) or self._salary_too_high(facts, rates)
        if category.kind == "hidden":
            return [*lines, "Decision: hidden (not a vacancy)"]
        skip = skip or self._group_off(self._group(facts, category, in_serbia))
        floor = self.salary_floor_rub(facts, rates)
        if facts.salary_text:
            lines.append(f"Salary «{facts.salary_text}»: floor ≈ {format_rub(floor)}/month" if floor is not None
                         else f"Salary «{facts.salary_text}»: no floor in a known currency")
        group, why = self._group_with_reason(facts, category, in_serbia)
        lines.append(f"Group {group}: {why}")
        stars, steps = self._stars_with_steps(facts, category, email_only)
        lines.append(f"Fit {stars}★: {steps}")
        permit, reason, rule = self._permit_decision(facts, in_serbia)
        lines.append(f"Permit {permit}: {rule}" + (f" — “{reason}”" if reason else ""))
        lines.append(f"Decision: skip — {skip}" if skip else "Decision: show (no skip rule matched)")
        return lines

    def _requirements_line(self, facts: VacancyFacts) -> str:
        places = {"text": "description", "title": "title", "label": "page data", "none": "nowhere"}
        years = (f"{facts.experience_years:g} years from the {places[facts.experience_source]}"
                 if facts.experience_years is not None else "no years")
        if facts.experience_years is not None and not facts.requirements_by_place:
            years = f"{facts.experience_years:g} years (scored before places were told apart)"
        found = ", ".join(f"{places[p]} {facts.experience_by[p]:g}" for p in facts.experience_by)
        grades = ", ".join(f"{places[p]} {facts.grade_by[p]}" for p in facts.grade_by)
        return (f"Requirements: {years}; grade {facts.seniority} from the {places[facts.seniority_source]}"
                + (f" (years found: {found})" if found else "") + (f" (grades found: {grades})" if grades else ""))

    def fit(self, facts: VacancyFacts) -> tuple[int, str]:
        """Stars and how they were counted (the settings screen shows them for every combination of facts)."""
        return self._stars_with_steps(facts, self._category(facts))

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
        too_long = self._too_much_experience(facts)
        if too_long:
            return too_long
        grade = self._grade_skip(facts)
        if grade:
            return grade
        russian_required = "ru" in facts.required_languages
        if facts.ad_language == "sr" and not russian_required:
            return "Ad in Serbian, Russian not required"
        # An ad written in Russian means Russian is fine there, even if the LLM listed only Serbian.
        if facts.serbian_required and not russian_required and facts.ad_language != "ru":
            return "Serbian required, Russian not required"
        if (
            facts.employer_type == "intl_english"
            and facts.english_working
            and facts.russian_speaking_team != "yes"
            and not in_serbia
        ):
            return "English-speaking company outside Serbia"
        if self._rules.drop_office_outside_serbia and self._office_abroad(facts, category, in_serbia):
            return "Office outside Serbia and Moscow" if self._rules.russia_office_cities else "Office outside Serbia"
        if published_at and now - published_at > timedelta(days=self._rules.max_age_days):
            return "Expired"
        return None

    def _group_off(self, group: int) -> str | None:
        info = self._rules.groups.get(group)
        return f"Group off: {info.title}" if info is not None and not info.enabled else None

    def _in_russia(self, facts: VacancyFacts) -> bool:
        return facts.country.strip().lower() in self._rules.russia_country_names

    def _grade_skip(self, facts: VacancyFacts) -> str | None:
        """The grade decides only when no place states the years (rules.yaml → requirements.years_over_grade)."""
        if self._rules.requirements.years_over_grade and facts.experience_years is not None:
            return None
        grade_sources = {"title", "label", "text"} if self._rules.drop_seniority_from_text else {"title", "label"}
        if facts.seniority in self._rules.dropped_seniority and facts.seniority_source in grade_sources:
            return f"Grade: {facts.seniority}"
        if self._grade_in_title(facts.title_original):
            return "Grade in the title"
        return None

    def _grade_in_title(self, title: str) -> bool:
        """The LLM may leave the grade unknown («Директор склада»); a junior marker keeps «Junior/Senior»."""
        if not self._title_grades or not self._title_grades.search(title):
            return False
        return not (self._title_juniors and self._title_juniors.search(title))

    def _too_much_experience(self, facts: VacancyFacts) -> str | None:
        years, limits = facts.experience_years, self._rules.experience_drop
        if years is None:
            return None
        junior = facts.seniority in {"intern", "junior"}
        limit = limits.years if junior or limits.years_unless_junior is None else limits.years_unless_junior
        return f"Experience: {years:g}+ years" if limit is not None and years >= limit else None

    def _office_abroad(self, facts: VacancyFacts, category: CategoryRule, in_serbia: bool) -> bool:
        country = facts.country.strip().lower()
        known_country = country not in {"", "unknown", "n/a", "remote"}
        if category.kind == "it" and self._russian_office_city(facts):
            return False  # IT in the office in Moscow (decision 15.11.2026): group 7
        return facts.work_format in {"office", "hybrid"} and known_country and not in_serbia

    def _russian_office_city(self, facts: VacancyFacts) -> bool:
        """In Russia and in one of rules.yaml → russia_office_cities; a city the ad does not name counts too."""
        if not self._in_russia(facts) or not self._rules.russia_office_cities:
            return False
        city = facts.city.strip().lower()
        return city in {"", "unknown"} or city in self._rules.russia_office_cities

    def _group(self, facts: VacancyFacts, category: CategoryRule, in_serbia: bool) -> int:
        return self._group_with_reason(facts, category, in_serbia)[0]

    def _group_with_reason(self, facts: VacancyFacts, category: CategoryRule, in_serbia: bool) -> tuple[int, str]:
        group, why = self._base_group(facts, category, in_serbia)
        target = self._rules.no_it_accreditation_group
        if target and group in self._rules.it_accreditation_groups and facts.accreditation == "no":
            return target, f"{why}; no IT accreditation: {facts.accreditation_note or 'not in the register'}"
        return group, why

    def _base_group(self, facts: VacancyFacts, category: CategoryRule, in_serbia: bool) -> tuple[int, str]:
        if facts.confidence == "low":
            return 5, "LLM confidence is low"
        is_it = category.kind == "it"
        is_manual = category.kind == "manual"
        if in_serbia and facts.employment in {"official", "unknown"} and facts.employer_type != "private_person":
            if is_it:
                return 1, f"IT in Serbia, employment {facts.employment}"
            if is_manual:
                return 2, f"manual work in Serbia, employment {facts.employment}"
        if is_manual and in_serbia and (
            facts.employment == "informal" or facts.employer_type == "private_person"
        ):
            return 4, f"manual work in Serbia, {facts.employment} / {facts.employer_type}"
        if is_it and facts.work_format in {"office", "hybrid"} and self._russian_office_city(facts):
            return 7, f"IT, {facts.work_format} in {facts.city or 'Russia'}"
        if is_it and facts.work_format in {"remote", "unknown"} and self._in_russia(facts):
            return 6, f"IT, format {facts.work_format}, country {facts.country}"
        if (
            is_it
            and facts.work_format in {"remote", "unknown"}
            and (facts.employer_type in RUSSIAN_EMPLOYERS or facts.russian_speaking_team == "yes")
        ):
            return 3, (f"IT, format {facts.work_format}, employer {facts.employer_type}, "
                       f"russian_speaking_team {facts.russian_speaking_team}")
        return 5, (f"no group rule matched (kind {category.kind}, format {facts.work_format}, "
                   f"employer {facts.employer_type}, russian_speaking_team {facts.russian_speaking_team})")

    def _stars(self, facts: VacancyFacts, category: CategoryRule, email_only: bool = False) -> int:
        return self._stars_with_steps(facts, category, email_only)[0]

    def _stars_with_steps(self, facts: VacancyFacts, category: CategoryRule, email_only: bool = False) -> tuple[int, str]:
        penalties = self._rules.penalties
        steps: list[str] = []
        minus = 0
        experienced = (
            facts.experience_years is not None
            and facts.experience_years >= penalties.experience_years_threshold
        )
        if category.kind == "it":
            if facts.stack_overlap == "partial":
                minus += penalties.stack_partial
                steps.append(f"−{penalties.stack_partial} partial stack")
            elif facts.stack_overlap == "none":
                minus += penalties.stack_none
                steps.append(f"−{penalties.stack_none} no stack overlap")
            if facts.english_working:
                minus += penalties.english_working
                steps.append(f"−{penalties.english_working} English working language")
        if category.kind in {"it", "manual"} and experienced:
            minus += penalties.experience
            steps.append(f"−{penalties.experience} {facts.experience_years:g}+ years required")
        if facts.ad_language == "ru" and penalties.russian_ad_bonus:
            minus -= penalties.russian_ad_bonus
            steps.append(f"+{penalties.russian_ad_bonus} ad in Russian")
        cap = category.cap
        english = facts.english_working or (facts.employer_type == "intl_english" and not facts.russian_link)
        if category.kind == "it" and english and penalties.english_working_max_stars is not None:
            cap = min(cap, penalties.english_working_max_stars)
            steps.append(f"English team cap {penalties.english_working_max_stars}")
        if email_only and penalties.email_only_max_stars is not None:
            cap = min(cap, penalties.email_only_max_stars)
            steps.append(f"E-mail only cap {penalties.email_only_max_stars}")
        stars = max(1, min(category.base - minus, cap))
        return stars, " ".join([f"base {category.base}", *steps, f"(cap {cap}, min 1)"])

    @classmethod
    def _residence_permit(cls, facts: VacancyFacts, in_serbia: bool) -> tuple[ResidencePermit, str]:
        permit, reason, _ = cls._permit_decision(facts, in_serbia)
        return permit, reason

    @staticmethod
    def _permit_decision(facts: VacancyFacts, in_serbia: bool) -> tuple[ResidencePermit, str, str]:
        reason = facts.serbian_entity_evidence
        if in_serbia and facts.visa_support == "yes":
            return "yes", reason or "residence permit support promised", "in Serbia + visa support"
        if in_serbia and facts.serbian_entity == "yes" and facts.employment == "official":
            return "yes", reason, "in Serbia + Serbian entity + official"
        if in_serbia and facts.employment in {"informal"}:
            return "no", reason or "informal", "informal employment"
        if facts.serbian_entity == "likely" or (in_serbia and facts.employment == "unknown"):
            return "likely", reason, f"serbian_entity {facts.serbian_entity}, employment {facts.employment}"
        if in_serbia and facts.employment == "official":
            return "likely", reason, f"in Serbia + official, serbian_entity {facts.serbian_entity}"
        return "no", reason, "not in Serbia or no Serbian entity"
