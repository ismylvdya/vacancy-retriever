from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Seniority = Literal["intern", "junior", "middle", "senior", "lead", "unknown"]
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

_DEFAULTS: dict[str, str] = {
    "seniority": "unknown",
    "seniority_source": "none",
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
}


class VacancyFacts(BaseModel):
    """Facts the LLM extracts from one vacancy. No judgement — code scores them."""

    model_config = ConfigDict(extra="ignore")

    title_ru: str = ""
    title_original: str = ""
    role_category: str = "other"
    seniority: Seniority = "unknown"
    seniority_source: SenioritySource = "none"
    experience_years: float | None = None
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
    serbian_entity: SerbianEntity = "unknown"
    serbian_entity_evidence: str = ""
    employment: Employment = "unknown"
    visa_support: VisaSupport = "not_mentioned"
    salary_text: str = ""
    summary_ru: str = ""
    link: str = ""
    confidence: Confidence = "low"

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

    @field_validator("experience_years", mode="before")
    @classmethod
    def _years(cls, value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "", "unknown") else None
        except (TypeError, ValueError):
            return None

    @field_validator(
        "title_ru", "title_original", "country", "city", "employer_name",
        "serbian_entity_evidence", "salary_text", "summary_ru", "link", "role_category",
        mode="before",
    )
    @classmethod
    def _text(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("serbian_required", "english_working", mode="before")
    @classmethod
    def _bool(cls, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value)

    @property
    def display_title(self) -> str:
        return self.title_original or self.title_ru or "Untitled"
