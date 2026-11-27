from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from datetime import time as dt_time
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .paths import PATHS, ProjectPaths


class ConfigError(Exception):
    """Human-readable configuration problem (typo in YAML, missing field, ...)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceConfig(_Strict):
    id: str
    type: str
    name: str
    url: str = ""
    enabled: bool = True
    max_pages: int = 5
    posts_per_day: float = 1.0
    icon: str | None = None
    disabled_reason: str | None = None
    groups: list[int] = Field(default_factory=list)  # feed groups it gives; none = unknown, always collected
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, value: str) -> str:
        if not value or not all(ch.isalnum() or ch in "_-" for ch in value):
            raise ValueError(f"id '{value}' must contain only letters, digits, '_' or '-'")
        return value


class SourcesFile(_Strict):
    sources: list[SourceConfig]
    detail_domains: list[str] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, sources: list[SourceConfig]) -> list[SourceConfig]:
        seen: set[str] = set()
        for source in sources:
            if source.id in seen:
                raise ValueError(f"duplicate source id '{source.id}'")
            seen.add(source.id)
        return sources


class CategoryRule(_Strict):
    label: str
    kind: Literal["it", "manual", "other", "drop", "hidden"]
    base: int = 1
    cap: int = 1
    drop_reason: str | None = None


class PenaltyRules(_Strict):
    stack_partial: int = 1
    stack_none: int = 2
    english_working: int = 1
    experience_years_threshold: float = 2
    experience: int = 1
    english_working_max_stars: int | None = None
    russian_ad_bonus: int = 0
    email_only_max_stars: int | None = None


class ExperienceDropRules(_Strict):
    years: float | None = None
    years_unless_junior: float | None = None


class RequirementRules(_Strict):
    # Where required experience and grade are looked for, the deciding place first (scoring/requirements.py):
    # text — the description, title — the title, label — the data above the description (tags, «Опыт работы»).
    priority: list[Literal["text", "title", "label"]] = Field(default_factory=lambda: ["text", "title", "label"])
    # Years found anywhere decide alone: the grade (dropped_seniority, grade words of the title) skips only a
    # vacancy that states no years at all.
    years_over_grade: bool = True


class NoAccreditationSigns(_Strict):
    # Signs that an employer surely has no IT accreditation, without the register (scoring/text_facts.py).
    employer_types: list[str] = Field(default_factory=list)   # facts.employer_type, e.g. private_person
    placeholder_employer: bool = False                         # the LLM says the name is a placeholder
    words: list[str] = Field(default_factory=list)            # in the text or the page's data; * = any ending


GROUP_NUMBERS = (1, 2, 3, 4, 5, 6, 7, 8)  # the groups scoring/engine.py can set


class GroupInfo(_Strict):
    title: str
    collapsed: bool = False
    note: str | None = None
    enabled: bool = True  # false: its vacancies are skipped ("Group off") and its own sources are not collected


class PrefilterRules(_Strict):
    title_grade_keywords: list[str] = Field(default_factory=list)
    title_junior_markers: list[str] = Field(default_factory=list)
    title_deny_keywords: list[str] = Field(default_factory=list)
    title_deny_reason: str = "Excluded by title keyword"
    # A title that announces a programme / wave is never dropped by keywords: the LLM reads it (docs/PROGRAMMES.md).
    title_programme_markers: list[str] = Field(default_factory=list)
    rvc_dropped_grades: list[str] = Field(default_factory=list)
    rvc_ad_markers: list[str] = Field(default_factory=list)
    # RVC: skip posts whose "required languages" line has no Russian and that don't mention Serbia.
    rvc_require_russian_or_serbia: bool = False
    rvc_serbia_markers: list[str] = Field(default_factory=list)


class SalaryRules(_Strict):
    max_floor_rub: int | None = None
    hours_per_month: int = 160


class Rules(_Strict):
    max_age_days: int = 30
    first_run_days: int = 7
    categories: dict[str, CategoryRule]
    penalties: PenaltyRules = Field(default_factory=PenaltyRules)
    dropped_seniority: list[str] = Field(default_factory=lambda: ["middle", "senior", "lead"])
    drop_seniority_from_text: bool = False
    experience_drop: ExperienceDropRules = Field(default_factory=ExperienceDropRules)
    requirements: RequirementRules = Field(default_factory=RequirementRules)
    russian_link_words: list[str] = Field(default_factory=list)
    salary: SalaryRules = Field(default_factory=SalaryRules)
    groups: dict[int, GroupInfo]
    group_order: list[int] = Field(default_factory=list)  # the feed's order; groups missing here follow by number
    prefilter: PrefilterRules = Field(default_factory=PrefilterRules)
    serbia_country_names: list[str] = Field(default_factory=lambda: ["serbia", "сербия", "srbija"])
    russia_country_names: list[str] = Field(default_factory=lambda: ["russia", "россия", "rf", "рф"])
    russia_office_cities: list[str] = Field(default_factory=list)  # IT in the office here is welcome (group 7)
    drop_office_outside_serbia: bool = False
    # IT accreditation (decision 20.11.2026): vacancies of these groups whose employer surely has no accreditation go
    # to `no_it_accreditation_group`; unknown accreditation stays where it is with a tag.
    it_accreditation_groups: list[int] = Field(default_factory=list)
    no_it_accreditation_group: int | None = None
    no_it_accreditation_signs: NoAccreditationSigns = Field(default_factory=NoAccreditationSigns)
    # Inside a feed group and among equal stars: these categories first, in this order; the rest after them.
    feed_category_order: list[str] = Field(default_factory=list)
    # Groups whose employer is Russian / Russian-speaking by definition: no «Russian-speaking» tag there (obvious).
    russian_tag_hidden_groups: list[int] = Field(default_factory=list)

    @field_validator("groups")
    @classmethod
    def _has_every_group(cls, value: dict[int, GroupInfo]) -> dict[int, GroupInfo]:
        missing = sorted(set(GROUP_NUMBERS) - value.keys())
        if missing:
            raise ValueError(f"groups {', '.join(map(str, missing))} are required (scoring/engine.py sets them)")
        return value

    @property
    def ordered_groups(self) -> list[int]:
        known = [n for n in dict.fromkeys(self.group_order) if n in self.groups]
        return known + sorted(n for n in self.groups if n not in known)

    @property
    def enabled_groups(self) -> list[int]:
        return [n for n in self.ordered_groups if self.groups[n].enabled]

    @model_validator(mode="after")
    def _known_categories_in_order(self) -> Rules:
        unknown = [c for c in self.feed_category_order if c not in self.categories]
        if unknown:
            raise ValueError(f"feed_category_order: unknown categories {', '.join(unknown)}")
        return self

    @field_validator("categories")
    @classmethod
    def _has_required_categories(cls, value: dict[str, CategoryRule]) -> dict[str, CategoryRule]:
        for required in ("not_a_vacancy", "other"):
            if required not in value:
                raise ValueError(f"category '{required}' is required")
        return value


class ModelPrice(_Strict):
    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


class StageModels(_Strict):
    scoring: str
    dedup: str
    resume: str | None = None  # CV + cover letter for the Apply sidebar; empty = the scoring model
    accreditation: str | None = None  # legal entities of an employer (IT accreditation); empty = the dedup model
    mail: str | None = None  # what an unread e-mail is (status of an application, internship wave…); empty = dedup


class ClaudeSdkSettings(_Strict):
    cli_path: str | None = None
    structured_output: bool = False
    timeout_seconds: int = 300
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    thinking: Literal["adaptive", "disabled"] | None = None
    pro_5h_limit_tokens: int | None = None


class CodexCliSettings(_Strict):
    cli_path: str | None = None
    timeout_seconds: int = Field(default=300, gt=0)
    effort: Literal["low", "medium", "high"] = "low"
    resume_effort: Literal["low", "medium", "high"] = "medium"


class ApiSettings(_Strict):
    base_url: str
    timeout_seconds: int = 120


class LlmConfig(_Strict):
    dedup_with_llm: bool = True
    provider: Literal["codex_cli", "claude_sdk", "anthropic_api", "openai_api"]
    models: dict[str, StageModels]
    batch_size: int = 10
    max_concurrency: int = 2
    max_input_chars: int = 4000
    max_input_chars_multi: int = 9000
    retries: int = 2
    codex_cli: CodexCliSettings = Field(default_factory=CodexCliSettings)
    claude_sdk: ClaudeSdkSettings = Field(default_factory=ClaudeSdkSettings)
    anthropic_api: ApiSettings = Field(
        default_factory=lambda: ApiSettings(base_url="https://api.anthropic.com")
    )
    openai_api: ApiSettings = Field(
        default_factory=lambda: ApiSettings(base_url="https://api.openai.com")
    )
    pricing: dict[str, ModelPrice] = Field(default_factory=dict)
    comparison_models: list[str] = Field(default_factory=list)

    @property
    def stage_models(self) -> StageModels:
        if self.provider not in self.models:
            raise ConfigError(f"llm.yaml: no models configured for provider '{self.provider}'")
        return self.models[self.provider]


class SenderRule(_Strict):
    match: str
    name: str
    parser: str = "generic"
    # Icon of the sender on cards: key of config/icons/mail-<icon>.* or of a favicon downloaded from `site`.
    icon: str | None = None
    site: str | None = None

    @property
    def icon_id(self) -> str | None:
        return f"mail-{self.icon}" if self.icon else None


class MailboxConfig(_Strict):
    id: str
    name: str
    kind: Literal["gmail", "yandex", "generic"]
    host: str
    port: int = 993
    login_env: str
    password_env: str
    enabled: bool = True
    folder: str = "INBOX"


class LinkedInVpn(_Strict):
    server: str = ""
    subscription_env: str = "LINKEDIN_VPN_SUBSCRIPTION"


class LinkedInExit(_Strict):
    kind: Literal["proxy", "vpn"] = "proxy"
    proxy_env: str = "LINKEDIN_PROXY_URL"


class LinkedInPublicPages(_Strict):
    enabled: bool = False
    exit: LinkedInExit = Field(default_factory=LinkedInExit)
    vpn: LinkedInVpn = Field(default_factory=LinkedInVpn)
    max_per_day: int = Field(default=12, ge=1)
    min_delay: float = Field(default=60.0, ge=1)
    max_delay: float = Field(default=180.0, ge=1)
    pause_days: int = Field(default=2, ge=1)
    delay_days: int = Field(default=1, ge=0)
    max_wait_days: int = Field(default=3, ge=1)


class JobSearchProvider(_Strict):
    id: Literal["jsearch", "serpapi"]
    key_env: str
    max_per_month: int = 50


class LinkedInDescriptions(_Strict):
    enabled: bool = False
    max_per_run: int = 40
    second_query: bool = True
    providers: list[JobSearchProvider] = Field(default_factory=list)


class MailTriage(_Strict):
    """Unread letters that are not vacancy alerts: statuses of applications, internship waves (docs/STATUSES.md)."""

    enabled: bool = True
    max_per_run: int = 80     # letters sent to the LLM in one run; the rest wait for the next run
    batch_size: int = 8


class MailConfig(_Strict):
    processed_label: str = "🦆 retrievered"
    triage: MailTriage = Field(default_factory=MailTriage)
    # Senders in the archive (user, 22.11.2026: LinkedIn): their letters are recognised by the From header only and
    # skipped before anything else — the text is not read, not triaged, not sent to the LLM, not marked or labelled.
    archived_senders: list[str] = Field(default_factory=list)
    max_age_days: int = 30
    linkedin_descriptions: LinkedInDescriptions = Field(default_factory=LinkedInDescriptions)
    linkedin_public_pages: LinkedInPublicPages = Field(default_factory=LinkedInPublicPages)
    mailboxes: list[MailboxConfig] = Field(default_factory=list)
    senders: list[SenderRule] = Field(default_factory=list)


class Secrets:
    """Values from config/.env. Never exported into os.environ."""

    def __init__(self, values: dict[str, str]):
        self._values = values

    @classmethod
    def load(cls, path: Path) -> Secrets:
        if not path.exists():
            return cls({})
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        return cls(values)

    def get(self, key: str) -> str | None:
        value = self._values.get(key)
        return value or None


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path.name}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: YAML syntax error: {exc}") from exc


def _parse[T: BaseModel](model: type[T], path: Path) -> T:
    try:
        return model.model_validate(_load_yaml(path))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{path.name}: {problems}") from exc


class BrowserSettings(_Strict):
    app: str | None = None
    shortcut_before_open: str | None = None

    @field_validator("shortcut_before_open")
    @classmethod
    def _shortcut(cls, value: str | None) -> str | None:
        if not value:
            return None
        *mods, key = [part.strip().lower() for part in value.split("+")]
        unknown = [m for m in mods if m not in {"ctrl", "cmd", "alt", "shift"}]
        if unknown or len(key) != 1:
            raise ValueError("use modifiers ctrl/cmd/alt/shift and one key, e.g. \"ctrl+2\"")
        return "+".join([*mods, key])


class UiConfig(_Strict):
    browser: BrowserSettings = Field(default_factory=BrowserSettings)


class WhereLabels(_Strict):
    ru_remote: str = "🇷🇺яз: удаленка"
    ru_serbia: str = "🇷🇺яз: в Сербии очно"
    ru_abroad: str = "🇷🇺яз: очно в 3-ей стране"
    ru_russia: str = "🇷🇺яз: очно в России"
    en_remote: str = "🇺🇸: удаленка"
    en_serbia: str = "🇺🇸: в Сербии очно"
    en_abroad: str = "🇺🇸: очно в 3-ей стране"
    en_russia: str = ""


class ObsidianSettings(_Strict):
    vault_dir: str | None = None
    folder: str = ""
    tags: list[str] = Field(default_factory=lambda: ["👾травля"])
    label_retrieved: str = ""   # a label in «👾лэйблы» for app-made notes; empty: the 🦆 at the end of the name says it
    app_mark: str = "🦆"        # at the end of the note name: «Data Analyst • Tribe 🦆» = made by the app
    what: str = "📋 вакансия"
    label_non_it: str = "📋не IT"
    status: str = "откликнулся"
    status_not_now: str = "не сейчас"  # Not now (N): a note with this status and the vacancy's skills only
    where: WhereLabels = Field(default_factory=WhereLabels)
    employer_what: str = "🧔🏻‍♂️ работодатель"


class ResumeSettings(_Strict):
    skill_dir: str = "~/.claude/skills/resume-tailor"
    tex_bin_dir: str | None = None
    max_skills: int = Field(default=10, ge=1, le=30)
    effort: Literal["low", "medium", "high"] | None = "medium"
    output_dir: str = "~/Downloads"  # where the CV PDF goes (nothing is kept in data/; user, 30.10.2026)


class DriveSettings(_Strict):
    enabled: bool = True
    stub: bool = False
    parent_required: bool = True   # vacancy folders go into the folder picked at /drive/pick
    picker_key_env: str = "GOOGLE_PICKER_API_KEY"
    folder_name: str = "{employer} • {title} • {date} 🦆"
    root_folder: str = "Vacancy Retriever"
    share_with_link: bool = True


class NameSettings(_Strict):
    """How the vacancy is named in the Obsidian note and the Drive folder (user, 28.10.2026)."""

    grade: bool = True              # «Junior …» / «Intern …» when the grade is intern / junior and the title lacks it
    ru_intern_suffix: str = "стажа"  # an internship at a Russian employer: «<должность> стажа» instead of «Intern»


class ApplyConfig(_Strict):
    enabled: bool = False
    names: NameSettings = Field(default_factory=NameSettings)
    start_delay_seconds: int = Field(default=8, ge=0)
    retry_minutes: int = Field(default=30, ge=1)
    day_starts_at: dt_time = dt_time(3, 0)  # sidebar: ▶ / N before this local time of today are «Previous days»
    obsidian: ObsidianSettings = Field(default_factory=ObsidianSettings)
    resume: ResumeSettings = Field(default_factory=ResumeSettings)
    drive: DriveSettings = Field(default_factory=DriveSettings)


class AccreditationConfig(_Strict):
    """config/accreditation.yaml — where the register comes from and how employers are looked up."""

    enabled: bool = True
    listing_url: str = "https://api.github.com/repos/DverkaSK/gosuslugi-itorgs/contents/data"
    refresh_days: int = 7
    dadata_key_env: str = "DADATA_API_KEY"
    max_employers_per_run: int = 160
    batch_size: int = 8
    recheck_unknown_days: int = 14


class AppConfig:
    """Lazy access to every file in config/. Each file is validated on first use."""

    def __init__(self, paths: ProjectPaths = PATHS):
        self.paths = paths

    @cached_property
    def sources(self) -> SourcesFile:
        return _parse(SourcesFile, self.paths.config / "sources.yaml")

    @cached_property
    def rules(self) -> Rules:
        return _parse(Rules, self.paths.config / "rules.yaml")

    @cached_property
    def llm(self) -> LlmConfig:
        return _parse(LlmConfig, self.paths.config / "llm.yaml")

    @cached_property
    def mail(self) -> MailConfig:
        return _parse(MailConfig, self.paths.config / "mail.yaml")

    @cached_property
    def ui(self) -> UiConfig:
        path = self.paths.config / "app.yaml"
        return _parse(UiConfig, path) if path.exists() else UiConfig()

    @cached_property
    def apply(self) -> ApplyConfig:
        path = self.paths.config / "apply.yaml"
        return _parse(ApplyConfig, path) if path.exists() else ApplyConfig()

    @cached_property
    def accreditation(self) -> AccreditationConfig:
        path = self.paths.config / "accreditation.yaml"
        return _parse(AccreditationConfig, path) if path.exists() else AccreditationConfig()

    def resolve_path(self, value: str) -> Path:
        """A path from a config file: absolute, ~/…, or relative to the project folder."""
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.paths.root / path

    @cached_property
    def secrets(self) -> Secrets:
        return Secrets.load(self.paths.config / ".env")

    @cached_property
    def profile(self) -> str:
        return (self.paths.config / "profile.md").read_text(encoding="utf-8")

    def prompt(self, name: str) -> str:
        return (self.paths.prompts / f"{name}.md").read_text(encoding="utf-8")

    def source(self, source_id: str) -> SourceConfig:
        for source in self.sources.sources:
            if source.id == source_id:
                return source
        raise ConfigError(f"Unknown source id '{source_id}'")

    def validate_all(self) -> None:
        _ = (self.sources, self.rules, self.llm, self.mail, self.ui, self.apply, self.accreditation, self.profile)
