from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Seniority = Literal["intern", "junior", "middle", "senior", "lead", "unknown"]
GRADE_ORDER = ("intern", "junior", "middle", "senior", "lead")
# Where a requirement was found: text — the description, title — the title, label — the data above the description
# (grade tags, «Опыт работы» of hh). The order that decides is rules.yaml → requirements.priority.
PLACES = ("text", "title", "label")
SenioritySource = Literal["title", "label", "text", "none"]
StackOverlap = Literal["strong", "partial", "none", "n_a"]
AdLanguage = Literal["ru", "en", "sr", "other"]
EnglishLevel = Literal["none", "basic", "b1", "b2", "c1_plus", "unknown"]
YesNoUnknown = Literal["yes", "no", "unknown"]
WorkFormat = Literal["office", "hybrid", "remote", "unknown"]
RemoteScope = Literal["worldwide", "russia_cis", "serbia_only", "unknown"]
EmployerType = Literal[
    "serbian",
    "ru_with_serbian_entity",
    "russian",
    "intl_russian_speaking",
    "intl_english",
    "private_person",
    "unknown",
]
SerbianEntity = Literal["yes", "likely", "no", "unknown"]
Employment = Literal["official", "informal", "freelance_contract", "unknown"]
VisaSupport = Literal["yes", "no", "not_mentioned"]
Confidence = Literal["high", "medium", "low"]

RUSSIAN_EMPLOYERS = {"russian", "ru_with_serbian_entity", "intl_russian_speaking"}

_DEFAULTS: dict[str, str] = {
    "seniority": "unknown",
    "seniority_source": "none",
    "experience_source": "none",
    "stack_overlap": "n_a",
    "ad_language": "other",
    "english_level": "unknown",
    "russian_speaking_team": "unknown",
    "work_format": "unknown",
    "remote_scope": "unknown",
    "employer_type": "unknown",
    "serbian_entity": "unknown",
    "employment": "unknown",
    "visa_support": "not_mentioned",
    "confidence": "low",
    "kind": "vacancy",
}


def to_years(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "", "unknown") else None
    except (TypeError, ValueError):
        return None


class VacancyFacts(BaseModel):
    """Facts the LLM extracts from one vacancy. No judgement — code scores them."""

    model_config = ConfigDict(extra="ignore")

    title_ru: str = ""
    title_original: str = ""
    role_category: str = "other"
    seniority: Seniority = "unknown"
    seniority_source: SenioritySource = "none"
    experience_years: float | None = None
    experience_source: SenioritySource = "none"
    # Per place (PLACES), from the LLM and, where it is silent, from code; scoring/requirements.py picks the deciding
    # one into seniority / experience_years. Empty in facts scored before 27.11.2026 (requirements_by_place false).
    experience_by: dict[str, float] = Field(default_factory=dict)
    grade_by: dict[str, Seniority] = Field(default_factory=dict)
    requirements_by_place: bool = False  # not from the LLM: set when the reply is parsed
    stack_overlap: StackOverlap = "n_a"
    matched_skills: list[str] = Field(default_factory=list)
    ad_language: AdLanguage = "other"
    required_languages: list[str] = Field(default_factory=list)
    serbian_required: bool = False
    english_level: EnglishLevel = "unknown"
    english_working: bool = False
    russian_speaking_team: YesNoUnknown = "unknown"
    country: str = "unknown"
    city: str = "unknown"
    work_format: WorkFormat = "unknown"
    remote_scope: RemoteScope = "unknown"
    employer_name: str = "unknown"
    employer_type: EmployerType = "unknown"
    # The «name» is a description, not a company: «крупный российский банк», «Digital-агентство».
    employer_placeholder: bool = False
    serbian_entity: SerbianEntity = "unknown"
    serbian_entity_evidence: str = ""
    employment: Employment = "unknown"
    visa_support: VisaSupport = "not_mentioned"
    salary_text: str = ""
    summary_ru: str = ""
    link: str = ""
    confidence: Confidence = "low"
    russian_mention: bool = False  # not from the LLM: the text has a word of rules.yaml → russian_link_words
    # Not from the scoring LLM: set after the run by accreditation/stage.py (register of accredited IT organisations).
    # None = not checked (yet). The engine moves «no» out of the Russian groups (rules.yaml → it_accreditation_groups).
    it_accreditation: Literal["yes", "no", "unknown"] | None = None
    it_accreditation_note: str = ""
    # Not from the LLM: why the vacancy surely has no IT accreditation without asking the register — a private person
    # or a sole proprietor, a placeholder employer, «ГПХ» / «самозанятый» in the text (rules.yaml →
    # no_it_accreditation_signals). Set again on every scoring and recalculation; "" = no such sign.
    no_accreditation_sign: str = ""
    # "programme": an internship / trainee programme, a school with hiring, an open recruitment wave — from ANY source
    # (post, letter, site), with or without concrete positions; never "not_a_vacancy" (docs/PROGRAMMES.md).
    kind: Literal["vacancy", "programme"] = "vacancy"
    # Not from the LLM: kind == "programme", or mail triage called the letter a wave. Shown as «Internship wave».
    wave: bool = False

    @property
    def russian_link(self) -> bool:
        """Any link to Russian: required, the ad is in it, a Russian-speaking team or employer, or the text mentions it."""
        return ("ru" in self.required_languages or self.ad_language == "ru" or self.russian_speaking_team == "yes"
                or self.employer_type in RUSSIAN_EMPLOYERS or self.russian_mention)

    @property
    def accreditation(self) -> Literal["yes", "no", "unknown"] | None:
        """The IT accreditation the feed acts on: a sign in the vacancy itself wins over the register check."""
        return "no" if self.no_accreditation_sign else self.it_accreditation

    @property
    def accreditation_note(self) -> str:
        return self.no_accreditation_sign or self.it_accreditation_note

    @property
    def english_team(self) -> bool:
        """No hint of Russian, and English is required, is the team's language, or the employer is international."""
        return not self.russian_link and ("en" in self.required_languages or self.english_working
                                          or self.employer_type == "intl_english")

    @field_validator(*_DEFAULTS.keys(), mode="before")
    @classmethod
    def _unknown_enum_to_default(cls, value: Any, info: Any) -> Any:
        allowed = cls.model_fields[info.field_name].annotation.__args__  # type: ignore[union-attr]
        if isinstance(value, str):
            value = value.strip().lower()
        return value if value in allowed else _DEFAULTS[info.field_name]

    @field_validator("required_languages", mode="before")
    @classmethod
    def _normalize_languages(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return sorted({str(item).strip().lower()[:2] for item in value if str(item).strip()})

    @field_validator("matched_skills", mode="before")
    @classmethod
    def _skills_list(cls, value: Any) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    @field_validator("experience_by", mode="before")
    @classmethod
    def _years_by_place(cls, value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        result = {}
        for place, years in value.items():
            numbers = years if isinstance(years, list) else [years]
            parsed = [to_years(n) for n in numbers]
            parsed = [n for n in parsed if n is not None]
            if place in PLACES and parsed:
                result[place] = min(parsed)
        return result

    @field_validator("grade_by", mode="before")
    @classmethod
    def _grades_by_place(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        result = {}
        for place, grades in value.items():
            names = [str(g).strip().lower() for g in (grades if isinstance(grades, list) else [grades])]
            known = [g for g in names if g in GRADE_ORDER]
            if place in PLACES and known:
                result[place] = min(known, key=GRADE_ORDER.index)
        return result

    @field_validator("experience_years", mode="before")
    @classmethod
    def _years(cls, value: Any) -> float | None:
        return to_years(value)

    @field_validator(
        "title_ru", "title_original", "country", "city", "employer_name",
        "serbian_entity_evidence", "salary_text", "summary_ru", "link", "role_category",
        mode="before",
    )
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("serbian_required", "english_working", "employer_placeholder", mode="before")
    @classmethod
    def _bool(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value)

    @property
    def display_title(self) -> str:
        return self.title_original or self.title_ru or "Untitled"
