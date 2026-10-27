from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cached_property
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..storage import Storage
from ..timeutil import utcnow
from .drive import CloudFolders
from .models import Application, ApplicationState, ApplyError, VacancyDossier
from .obsidian import SKILLS_PROPERTY, ObsidianVault, note_file_name
from .resume import MaterialsWriter, ResumeSkill, insert_cv_link

log = logging.getLogger(__name__)

RUSSIAN_EMPLOYERS = {"russian", "ru_with_serbian_entity", "intl_russian_speaking"}
SERBIA = {"serbia", "srbija", "rs", "сербия"}
_APPLY_LINE = re.compile(r"^Apply: (https?://\S+)$", re.M)


class DossierBuilder:
    """Vacancy → the facts the note, the folder and the LLM need. Reads storage only."""

    def __init__(self, config: AppConfig, storage: Storage):
        self._config = config
        self._storage = storage

    def build(self, vacancy_id: int) -> VacancyDossier:
        record = self._storage.vacancies.get(vacancy_id)
        if record is None:
            raise ApplyError(f"Vacancy #{vacancy_id} not found")
        raw = self._storage.items.get(record.primary_raw_id)
        text = raw.full_text.strip() if raw else record.title
        facts = record.facts
        links = [s.url for s in record.sources if s.url]
        if facts and facts.link:
            links.append(facts.link)
        links += _APPLY_LINE.findall(text)
        employer = record.employer or (facts.employer_name if facts and facts.employer_name != "unknown" else "")
        hint = ""
        if facts:
            hint = (f"Location: {facts.city}, {facts.country}; format: {facts.work_format}; "
                    f"employer type: {facts.employer_type}; ad language: {facts.ad_language}")
        return VacancyDossier(vacancy_id=record.id, title=(facts.display_title if facts else "") or record.title,
                              employer=employer, text=text, links=list(dict.fromkeys(links)),
                              where=self.where(record.facts), labels=self.labels(record.category), company_hint=hint)

    def where(self, facts: Any) -> list[str]:
        labels = self._config.apply.obsidian.where
        if facts is None:
            return []
        russian = (facts.ad_language == "ru" or "ru" in facts.required_languages
                   or facts.russian_speaking_team == "yes" or facts.employer_type in RUSSIAN_EMPLOYERS)
        in_serbia = facts.country.casefold() in SERBIA
        if facts.work_format == "remote":
            place = "remote"
        elif in_serbia:
            place = "serbia"
        elif facts.work_format in ("office", "hybrid") and facts.country not in ("", "unknown"):
            place = "abroad"
        else:
            return []
        return [getattr(labels, f"{'ru' if russian else 'en'}_{place}")]

    def labels(self, category: str) -> list[str]:
        rule = self._config.rules.categories.get(category)
        return [self._config.apply.obsidian.label_non_it] if rule and rule.kind == "manual" else []


@dataclass
class ApplyServices:
    """What the steps work with; shared by all applications."""

    builder: DossierBuilder
    vault: ObsidianVault | None
    drive: CloudFolders
    writer: MaterialsWriter
    skill: ResumeSkill
    out_dir: Path


@dataclass
class StepContext:
    """One pass over one application."""

    vacancy_id: int
    services: ApplyServices
    warnings: list[str] = field(default_factory=list)

    @cached_property
    def dossier(self) -> VacancyDossier:
        return self.services.builder.build(self.vacancy_id)

    @property
    def vault(self) -> ObsidianVault | None:
        return self.services.vault

    @property
    def drive(self) -> CloudFolders:
        return self.services.drive

    @property
    def writer(self) -> MaterialsWriter:
        return self.services.writer

    @property
    def skill(self) -> ResumeSkill:
        return self.services.skill

    @property
    def out_dir(self) -> Path:
        return self.services.out_dir


class Step(ABC):
    name: str

    @abstractmethod
    def needed(self, app: Application, ctx: StepContext) -> bool: ...

    @abstractmethod
    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]: ...


class NoteStep(Step):
    name = "note"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return ctx.vault is not None and not app.note_path

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        assert ctx.vault is not None
        return {"note_path": str(ctx.vault.create(ctx.dossier))}


class FolderStep(Step):
    name = "folder"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return ctx.drive.available and not app.folder_id

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        item = ctx.drive.ensure_folder(app.vacancy_id, note_file_name(ctx.dossier.title, ctx.dossier.employer))
        return {"folder_id": item.id, "folder_url": item.url}


class MaterialsStep(Step):
    name = "materials"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return not app.materials_json

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        materials = asyncio.run(ctx.writer.write(ctx.dossier))
        return {"materials_json": materials.to_json(), "skills": ", ".join(materials.skills)}


class PdfStep(Step):
    name = "pdf"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return not (app.pdf_path and Path(app.pdf_path).exists())

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        assert app.materials is not None
        pdf, warning = ctx.skill.build(app.materials.selection, ctx.out_dir / str(app.vacancy_id))
        if warning:
            log.warning("Application %s: %s", app.id, warning)
        return {"pdf_path": str(pdf), "pdf_file_id": None, "pdf_url": None}


class UploadStep(Step):
    name = "upload"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return ctx.drive.available and bool(app.folder_id) and not app.pdf_file_id

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        assert app.folder_id and app.pdf_path
        item = ctx.drive.upload_pdf(app.vacancy_id, app.folder_id, Path(app.pdf_path))
        return {"pdf_file_id": item.id, "pdf_url": item.url}


class FinalizeStep(Step):
    """Always runs (cheap, idempotent): the letter gets the CV link, the note gets the letter, skills and folder."""

    name = "finalize"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return app.materials is not None

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        assert app.materials is not None
        letter = insert_cv_link(app.materials.letter, app.pdf_url)
        updates: dict[str, Any] = {"letter": letter}
        note = ctx.vault.find(app.vacancy_id, app.note_path) if ctx.vault else None
        if note is not None:
            ctx.vault.set_block(note, "letter", ctx.vault.letter_block(letter))  # type: ignore[union-attr]
            ctx.vault.set_block(note, "folder", ctx.vault.folder_block(app.folder_url, app.pdf_url))  # type: ignore[union-attr]
            if app.skills:
                ctx.vault.fill_property(note, SKILLS_PROPERTY, app.skills)  # type: ignore[union-attr]
            updates["note_path"] = str(note)
        elif ctx.vault is not None and app.note_path:
            ctx.warnings.append("The Obsidian note was not found (deleted?)")
        return updates


STEPS: list[Step] = [NoteStep(), FolderStep(), MaterialsStep(), PdfStep(), UploadStep(), FinalizeStep()]


class ApplicationPipeline:
    """Runs the steps that are not done yet. Any stop (restart, limit, error) resumes at the first undone step."""

    def __init__(self, storage: Storage, services: ApplyServices, retry_minutes: int, steps: list[Step] | None = None):
        self._repo = storage.applications
        self._services = services
        self._retry = timedelta(minutes=retry_minutes)
        self._steps = steps or STEPS

    def process(self, app: Application) -> Application:
        ctx = StepContext(app.vacancy_id, self._services)
        for step in self._steps:
            current = self._repo.get(app.id)
            if current is None:
                return app
            app = current
            if app.cancel_requested:
                self._repo.update(app.id, state=ApplicationState.CANCELLED, step=None)
                return self._repo.get(app.id) or app
            if not step.needed(app, ctx):
                continue
            self._repo.update(app.id, state=ApplicationState.WORKING, step=step.name, error=None)
            try:
                updates = step.run(app, ctx)
            except ApplyError as exc:
                return self._stop(app, step, exc)
            except Exception as exc:  # one bad vacancy must not stop the worker
                log.exception("Application %s: step %s failed", app.id, step.name)
                return self._stop(app, step, ApplyError(f"{type(exc).__name__}: {exc}"))
            self._repo.update(app.id, **updates)
        self._repo.update(app.id, state=ApplicationState.DONE, step=None, error=None,
                          warning="; ".join(self._warnings(ctx)) or None)
        return self._repo.get(app.id) or app

    def _warnings(self, ctx: StepContext) -> list[str]:
        warnings = list(ctx.warnings)
        if ctx.vault is None:
            warnings.append("Obsidian vault not found (config/apply.yaml → obsidian.vault_dir)")
        if not ctx.drive.available:
            warnings.append(ctx.drive.reason or "Google Drive is not connected")
        return warnings

    def _stop(self, app: Application, step: Step, exc: ApplyError) -> Application:
        if exc.kind == "limit":
            self._repo.update(app.id, state=ApplicationState.WAITING, step=step.name, error=str(exc),
                              due_at=utcnow() + self._retry)
        else:
            self._repo.update(app.id, state=ApplicationState.FAILED, step=step.name, error=str(exc))
        log.warning("Application %s stopped at %s: %s", app.id, step.name, exc)
        return self._repo.get(app.id) or app
