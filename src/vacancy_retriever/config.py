from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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


class GroupInfo(_Strict):
    title: str
    collapsed: bool = False
    note: str | None = None


class PrefilterRules(_Strict):
    title_grade_keywords: list[str] = Field(default_factory=list)
    title_junior_markers: list[str] = Field(default_factory=list)
    title_deny_keywords: list[str] = Field(default_factory=list)
    title_deny_reason: str = "Excluded by title keyword"
    rvc_dropped_grades: list[str] = Field(default_factory=list)
    rvc_ad_markers: list[str] = Field(default_factory=list)
    # RVC: skip posts whose "required languages" line has no Russian and that don't mention Serbia.
    rvc_require_russian_or_serbia: bool = False
    rvc_serbia_markers: list[str] = Field(default_factory=list)


class Rules(_Strict):
    max_age_days: int = 30
    first_run_days: int = 7
    categories: dict[str, CategoryRule]
    penalties: PenaltyRules = Field(default_factory=PenaltyRules)
    dropped_seniority: list[str] = Field(default_factory=lambda: ["middle", "senior", "lead"])
    groups: dict[int, GroupInfo]
    prefilter: PrefilterRules = Field(default_factory=PrefilterRules)
    serbia_country_names: list[str] = Field(default_factory=lambda: ["serbia", "сербия", "srbija"])
    drop_office_outside_serbia: bool = False

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


class ClaudeSdkSettings(_Strict):
    cli_path: str | None = None
    structured_output: bool = False
    timeout_seconds: int = 300
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    thinking: Literal["adaptive", "disabled"] | None = None
    pro_5h_limit_tokens: int | None = None


class ApiSettings(_Strict):
    base_url: str
    timeout_seconds: int = 120


class LlmConfig(_Strict):
    provider: Literal["claude_sdk", "anthropic_api", "openai_api"]
    models: dict[str, StageModels]
    batch_size: int = 10
    max_concurrency: int = 2
    max_input_chars: int = 4000
    max_input_chars_multi: int = 9000
    retries: int = 2
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


class MailConfig(_Strict):
    processed_label: str = "🦆 retrievered"
    max_age_days: int = 30
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
        _ = self.sources, self.rules, self.llm, self.mail, self.ui, self.profile
