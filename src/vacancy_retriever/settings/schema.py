"""What the settings screen shows: sections of fields, each bound to one value in one config file.

Adding a setting = one Field here (file, dotted path, kind, label, help). The screen renders it by its kind; the
service writes it back with yaml_patch and validates the whole file with the same pydantic model as at start-up.
Help texts are for the user (English, like the rest of the interface); the YAML comments stay the reference.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FieldKind = Literal["toggle", "number", "stars", "choice", "chips", "text"]
CONFIG_FILES = ("rules", "sources", "mail", "llm", "apply")


@dataclass(frozen=True)
class Field:
    file: str                 # config/<file>.yaml
    path: str                 # yaml_patch path: «penalties.stack_none», «sources[gmail].enabled»
    kind: FieldKind
    label: str
    help: str = ""
    unit: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    nullable: bool = False    # an empty number = «no limit» (written as null)
    options: tuple[tuple[str, str], ...] = ()  # (value, label) for choice / chips with a fixed set

    @property
    def id(self) -> str:
        return f"{self.file}:{self.path}"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.id
        data["options"] = [{"value": v, "label": label} for v, label in self.options]
        return data


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    intro: str
    fields: tuple[Field, ...] = ()
    widget: str = ""          # a custom block of the screen: «fit» (categories tree + matrix), «sources», «groups»
    groups: tuple[tuple[str, tuple[Field, ...]], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "intro": self.intro, "widget": self.widget,
                "fields": [f.as_dict() for f in self.fields],
                "groups": [{"title": title, "fields": [f.as_dict() for f in fields]} for title, fields in self.groups]}


GRADES = (("intern", "Intern"), ("junior", "Junior"), ("middle", "Middle"), ("senior", "Senior"), ("lead", "Lead"))
KINDS = (("it", "IT / data"), ("manual", "Hands-on"), ("other", "Other"), ("drop", "Skipped"), ("hidden", "Not a vacancy"))
EFFORT = (("low", "Low"), ("medium", "Medium"), ("high", "High"))
CLAUDE_MODELS = (("haiku", "Haiku"), ("sonnet", "Sonnet"), ("opus", "Opus"))


def _penalty(path: str, label: str, help_: str) -> Field:
    return Field("rules", f"penalties.{path}", "number", label, help_, unit="★", min=0, max=4, step=1)


SECTIONS: tuple[Section, ...] = (
    Section(
        "fit", "Fit (stars)",
        "How many stars a vacancy gets from the facts the LLM extracted. Each role category has a base and a ceiling; "
        "penalties take stars away. Pick a category to see the stars of every combination of facts.",
        widget="fit",
        fields=(
            _penalty("stack_partial", "Stack partly matches", "IT roles: the vacancy's stack overlaps your skills only in part."),
            _penalty("stack_none", "Stack does not match", "IT roles: no overlap with your skills."),
            _penalty("english_working", "English working language", "IT roles: the team works in English."),
            Field("rules", "penalties.english_working_max_stars", "stars", "English team: at most",
                  "However well the rest matches, an English-speaking team gets no more stars than this."),
            Field("rules", "penalties.experience_years_threshold", "number", "Experience counts from",
                  "Required experience from this many years takes a star away.", unit="years", min=0, max=10, step=0.5),
            _penalty("experience", "Experience required", "IT and hands-on roles."),
            Field("rules", "penalties.russian_ad_bonus", "number", "Ad in Russian adds",
                  "All else equal, an ad written in Russian gets this many stars more (never above the category's cap).",
                  unit="★", min=0, max=4, step=1),
            Field("rules", "penalties.email_only_max_stars", "stars", "E-mail only: at most",
                  "Known only from a job-alert letter, no description: the evaluation is less reliable."),
        ),
    ),
    Section(
        "feed", "What is skipped",
        "Rules that send a vacancy to Processed → Auto-skipped. They are applied by code, without the LLM.",
        groups=(
            ("Grade and age", (
                Field("rules", "dropped_seniority", "chips", "Skip these grades", options=GRADES,
                      help="When the grade is in the title or the grade label of the ad."),
                Field("rules", "drop_seniority_from_text", "toggle", "Also when the grade is only in the text",
                      "«We are looking for a Senior …» in the body of the ad. Experience in years never counts as a grade."),
                Field("rules", "max_age_days", "number", "Older than", "Such vacancies leave the feed as Expired.",
                      unit="days", min=1, max=90, step=1),
                Field("rules", "first_run_days", "number", "A new source starts with the last", unit="days",
                      min=1, max=30, step=1),
            )),
            ("Place and money", (
                Field("rules", "drop_office_outside_serbia", "toggle", "Skip office / hybrid outside Serbia",
                      "Only when the country is known. Remote work and unknown countries stay."),
                Field("rules", "salary.max_floor_rub", "number", "Skip when the salary floor is above",
                      "Per month, converted at the rate of the day it was collected. Empty — no limit.",
                      unit="₽ / month", min=0, step=5000, nullable=True),
                Field("rules", "salary.hours_per_month", "number", "Hours in a month (hourly rates)", unit="h",
                      min=40, max=300, step=8),
            )),
        ),
    ),
    Section("groups", "Feed groups", "Titles and notes of the five groups of the feed, in this order.", widget="groups"),
    Section(
        "prefilter", "Pre-filter (no LLM)",
        "Cheap rules applied to single vacancies (site cards, RVC posts) before any LLM call. Whole words, any case.",
        fields=(
            Field("rules", "prefilter.title_grade_keywords", "chips", "Grade words in the title — skip",
                  "Not applied when the title also has a junior marker below."),
            Field("rules", "prefilter.title_junior_markers", "chips", "Junior markers — keep"),
            Field("rules", "prefilter.title_deny_keywords", "chips", "Roles that never fit — skip"),
            Field("rules", "prefilter.rvc_dropped_grades", "chips", "RVC channels: skipped grades"),
            Field("rules", "prefilter.rvc_require_russian_or_serbia", "toggle",
                  "RVC channels: skip posts without Russian and without Serbia",
                  "Saves about a third of the LLM work on RVC Global."),
        ),
    ),
    Section("sources", "Sources", "Which sources a run collects from. A disabled source keeps its history.",
            widget="sources"),
    Section(
        "linkedin", "LinkedIn: Google for Jobs",
        "LinkedIn letters carry only a title. The description comes from Google for Jobs, with no requests to LinkedIn; "
        "what it does not find is evaluated from the letter.",
        groups=(
            ("Google for Jobs", (
                Field("mail", "linkedin_descriptions.enabled", "toggle", "Look up descriptions",
                      "JSearch, then SerpApi. No requests to LinkedIn."),
                Field("mail", "linkedin_descriptions.max_per_run", "number", "API requests per run", min=1, max=200, step=1),
            )),
        ),
    ),
    # The switch itself is not a Field: it has its own endpoints (see SettingsService.linkedin_pages_on).
    Section(
        "linkedin-pages", "LinkedIn pages (proxy)",
        "The second way to get a description: LinkedIn's public page, read through a private proxy, for what Google for "
        "Jobs did not find. It is the only part of the app that ever talks to LinkedIn.",
        widget="linkedin_pages",
        groups=(
            ("Limits while it is on", (
                Field("mail", "linkedin_public_pages.max_per_day", "number", "Pages per day", min=1, max=30, step=1),
                Field("mail", "linkedin_public_pages.min_delay", "number", "Pause between pages from", unit="s",
                      min=30, max=600, step=10),
                Field("mail", "linkedin_public_pages.max_delay", "number", "… to", unit="s", min=30, max=900, step=10),
                Field("mail", "linkedin_public_pages.delay_days", "number", "Not earlier than after the letter",
                      unit="days", min=0, max=7, step=1),
                Field("mail", "linkedin_public_pages.max_wait_days", "number", "A vacancy waits at most", unit="days",
                      min=1, max=14, step=1),
                Field("mail", "linkedin_public_pages.pause_days", "number", "Pause after LinkedIn refuses", unit="days",
                      min=1, max=14, step=1),
            )),
        ),
    ),
    Section(
        "llm", "LLM",
        "Which Claude model does what (subscription through Claude Code). Facts need little: Haiku is enough.",
        fields=(
            Field("llm", "models.claude_sdk.scoring", "choice", "Facts from vacancies", options=CLAUDE_MODELS),
            Field("llm", "models.claude_sdk.dedup", "choice", "Same vacancy?", options=CLAUDE_MODELS),
            Field("llm", "models.claude_sdk.resume", "choice", "CV and cover letter", options=CLAUDE_MODELS),
            Field("llm", "batch_size", "number", "Vacancies per request", min=1, max=30, step=1),
            Field("llm", "max_concurrency", "number", "Requests at once", "Each one is a Claude Code process.",
                  min=1, max=4, step=1),
            Field("llm", "dedup_with_llm", "toggle", "Ask the LLM about similar pairs",
                  "Off: only identical title + company are merged (free)."),
        ),
    ),
    Section(
        "apply", "Apply",
        "What ▶ does: the Obsidian note, the tailored CV and cover letter, the Google Drive folder (docs/APPLY.md).",
        fields=(
            Field("apply", "enabled", "toggle", "▶ makes the CV, letter, note and folder"),
            Field("apply", "start_delay_seconds", "number", "Wait before starting (time to Undo)", unit="s",
                  min=0, max=60, step=1),
            Field("apply", "retry_minutes", "number", "Retry after a Claude limit in", unit="min", min=5, max=300, step=5),
            Field("apply", "resume.effort", "choice", "Effort for the CV and letter", options=EFFORT),
            Field("apply", "drive.enabled", "toggle", "Google Drive folder"),
        ),
    ),
)


def all_fields() -> dict[str, Field]:
    fields: dict[str, Field] = {}
    for section in SECTIONS:
        for f in section.fields:
            fields[f.id] = f
        for _, group in section.groups:
            for f in group:
                fields[f.id] = f
    return fields
