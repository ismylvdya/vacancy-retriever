from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..timeutil import from_iso


class ApplicationState(StrEnum):
    DRAFT = "draft"          # ▶ put it in the sidebar's purgatory: nothing is made until Generate (18.11.2026)
    QUEUED = "queued"
    WORKING = "working"
    WAITING = "waiting"      # stopped by the Claude limit; continues at due_at
    FAILED = "failed"        # needs Retry
    DONE = "done"
    CANCELLED = "cancelled"  # the user undid ▶ after the work had started

    @property
    def active(self) -> bool:
        return self in (ApplicationState.QUEUED, ApplicationState.WORKING, ApplicationState.WAITING)


class ApplyError(Exception):
    """A step failed. `kind`: limit (wait and retry) | setup (the user must configure something) | other."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Materials:
    """What the LLM chose by the resume-tailor skill: CV selection for build.py, the letter, the vacancy's skills."""

    selection: dict[str, Any]
    letter: str
    skills: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"selection": self.selection, "letter": self.letter, "skills": self.skills},
                          ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Materials:
        data = json.loads(text)
        return cls(data["selection"], data["letter"], list(data.get("skills") or []))


@dataclass
class Application:
    id: int
    vacancy_id: int
    state: ApplicationState
    step: str | None
    due_at: datetime | None
    note_path: str | None = None
    folder_id: str | None = None
    folder_url: str | None = None
    materials_json: str | None = None
    pdf_path: str | None = None
    pdf_file_id: str | None = None
    pdf_url: str | None = None
    letter: str | None = None
    skills: str | None = None
    error: str | None = None
    warning: str | None = None
    cancel_requested: bool = False
    dismissed: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    kind: str = "apply"           # apply (▶) | not_now (N: the Obsidian note only, with the vacancy's skills)
    with_cv: bool = True          # Generate: the CV (+ the Drive folder)
    with_letter: bool = True      # Generate: the cover letter (IT: only with the CV, it carries the CV's link)
    note_mine: str | None = None  # what the app last wrote into «### моё»
    stats_json: str | None = None
    generation: int = 1

    @property
    def drive_key(self) -> int | str:
        if self.generation == 1:
            return self.vacancy_id
        return f"{self.vacancy_id}-{self.generation}"

    @property
    def stats(self) -> dict[str, Any]:
        try:
            return json.loads(self.stats_json) if self.stats_json else {}
        except ValueError:
            return {}

    @property
    def materials(self) -> Materials | None:
        return Materials.from_json(self.materials_json) if self.materials_json else None

    @property
    def started(self) -> bool:
        return bool(self.note_path or self.folder_id or self.materials_json or self.pdf_path)

    @classmethod
    def from_row(cls, row: Any) -> Application:
        return cls(
            id=row["id"], vacancy_id=row["vacancy_id"], state=ApplicationState(row["state"]), step=row["step"],
            due_at=from_iso(row["due_at"]), note_path=row["note_path"], folder_id=row["folder_id"],
            folder_url=row["folder_url"], materials_json=row["materials_json"], pdf_path=row["pdf_path"],
            pdf_file_id=row["pdf_file_id"], pdf_url=row["pdf_url"], letter=row["letter"], skills=row["skills"],
            error=row["error"], warning=row["warning"], cancel_requested=bool(row["cancel_requested"]),
            dismissed=bool(row["dismissed"]), created_at=from_iso(row["created_at"]),
            updated_at=from_iso(row["updated_at"]), kind=row["kind"] or "apply", note_mine=row["note_mine"],
            with_cv=bool(row["with_cv"]), with_letter=bool(row["with_letter"]),
            stats_json=row["stats_json"], generation=row["generation"],
        )


@dataclass
class VacancyDossier:
    """Everything the steps need to know about one vacancy, gathered from storage once."""

    vacancy_id: int
    title: str
    employer: str
    text: str
    links: list[tuple[str, str]]  # (label, url): «эта вакансия на сайте LinkedIn» / «… на сайте <работодателя>»
    where: list[str]
    labels: list[str]
    company_hint: str = ""
    name_title: str = ""  # the title for the note / folder names: with the grade (DossierBuilder.name_title)  # e.g. the country, helps the LLM choose photo / language
    manual: bool = False  # hands-on work (kind: manual in rules.yaml): the note and a letter, no CV and no Drive
