from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
from urllib.parse import urlparse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..storage import Storage
from ..timeutil import utcnow
from .drive import CloudFolders, StubDrive
from .description import LetterDescriptions
from .models import Application, ApplicationState, ApplyError, VacancyDossier
from .obsidian import SKILLS_PROPERTY, ObsidianVault
from .resume import MaterialsWriter, ResumeSkill, insert_cv_link

log = logging.getLogger(__name__)

APPLY, NOT_NOW = "apply", "not_now"  # applications.kind: ▶ (CV, letter, Drive; hands-on work: the letter only) / N (the note with skills only)

RUSSIAN_EMPLOYERS = {"russian", "ru_with_serbian_entity", "intl_russian_speaking"}
SERBIA = {"serbia", "srbija", "rs", "сербия"}
_GRADE_IN_TITLE = re.compile(r"\b(intern|internship|junior|jr|trainee|graduate|entry[- ]level)\b|стаж|младш", re.I)
_APPLY_LINE = re.compile(r"^Apply: (https?://\S+)$", re.M)
# A Latin part counts as the English name of the role only when it names a role: «Аналитик данных (Data Analyst)» →
# «Data Analyst», but «Менеджер продукта (Computer Vision)» stays as it is (the user, 18.11.2026).
_ROLE_WORD = re.compile(r"\b(analyst|engineer|scientist|developer|programmer|manager|designer|specialist|architect|"
                        r"researcher|consultant|tester|intern|trainee|lead|administrator|operator|assistant|"
                        r"modeler|strategist|owner|officer)s?\b", re.I)
_CYRILLIC = re.compile(r"[а-яё]", re.I)
_LATIN = re.compile(r"[a-z]", re.I)


def english_title(title: str) -> str:
    """One language in the note / folder name: «Аналитик данных (Data Analyst)», «BI Developer/BI разработчик»,
    «Технический проджект менеджер / Technical project manager» → the English role. Anything else is kept."""
    def english_role(part: str) -> bool:
        return bool(_ROLE_WORD.search(part)) and not _CYRILLIC.search(part) and bool(_LATIN.search(part))

    match = re.fullmatch(r"\s*([^()]+?)\s*\(([^()]+)\)\s*", title)
    if match and _CYRILLIC.search(match.group(1)) and english_role(match.group(2)):
        return match.group(2).strip()
    depth, cuts = 0, []
    for index, char in enumerate(title):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "/" and depth == 0:
            cuts.append(index)
    if len(cuts) == 1:
        parts = [title[:cuts[0]].strip(), title[cuts[0] + 1:].strip()]
        english = [p for p in parts if english_role(p)]
        if len(english) == 1 and any(_CYRILLIC.search(p) for p in parts if p not in english):
            return english[0]
    return title


class DossierBuilder:
    """Vacancy → the facts the note, the folder and the LLM need. Reads storage only."""

    def __init__(self, config: AppConfig, storage: Storage):
        self._config = config
        self._storage = storage
        self._descriptions = LetterDescriptions(config, storage)

    def build(self, vacancy_id: int) -> VacancyDossier:
        record = self._storage.vacancies.get(vacancy_id)
        if record is None:
            raise ApplyError(f"Vacancy #{vacancy_id} not found")
        raw = self._storage.items.get(record.primary_raw_id)
        text = ((raw and self._descriptions.complete(raw)) or raw.full_text).strip() if raw else record.title
        facts = record.facts
        employer_name = record.employer or (facts.employer_name if facts and facts.employer_name != "unknown" else "")
        sources = {s.id: s for s in self._config.sources.sources}
        links = [(self._source_label(sources.get(s.source_id), s.url), s.url) for s in record.sources if s.url]
        employer_label = f"эта вакансия на сайте {employer_name}" if employer_name else "эта вакансия на сайте работодателя"
        if facts and facts.link:
            links.append((self._source_label(None, facts.link), facts.link))
        links += [(employer_label, url) for url in _APPLY_LINE.findall(text)]  # employer pages from Google for Jobs
        unique: dict[str, str] = {}
        for label, url in links:
            unique.setdefault(url, label)
        employer = record.employer or (facts.employer_name if facts and facts.employer_name != "unknown" else "")
        hint = ""
        if facts:
            hint = (f"Location: {facts.city}, {facts.country}; format: {facts.work_format}; "
                    f"employer type: {facts.employer_type}; ad language: {facts.ad_language}")
        title = (facts.display_title if facts else "") or record.title
        return VacancyDossier(vacancy_id=record.id, title=title, name_title=self.name_title(english_title(title), facts),
                              employer=employer, text=text, links=[(label, url) for url, label in unique.items()],
                              where=self.where(record.facts), labels=self.labels(record.category), company_hint=hint,
                              manual=self.manual(record.category))

    def name_title(self, title: str, facts: Any) -> str:
        """«Junior Data Analyst», «Intern ML Engineer»; an internship at a Russian employer — «Аналитик данных стажа».
        Nothing is added when the title already says it (Junior, Intern, Trainee, стаж…, младш…)."""
        names = self._config.apply.names
        if not names.grade or facts is None or facts.seniority not in ("intern", "junior"):
            return title
        if facts.seniority == "intern" and facts.employer_type in RUSSIAN_EMPLOYERS and names.ru_intern_suffix:
            return title if re.search(r"стаж", title, re.I) else f"{title} {names.ru_intern_suffix}"
        if _GRADE_IN_TITLE.search(title):
            return title
        return f"{'Intern' if facts.seniority == 'intern' else 'Junior'} {title}"

    @staticmethod
    def _source_label(source: Any, url: str) -> str:
        """«эта вакансия на сайте LinkedIn» / «… на сайте Infostud» / «эта вакансия в Telegram-канале Serbia Work»."""
        if source is not None and source.type == "telegram":
            return f"эта вакансия в Telegram-канале {source.name}"
        if source is not None and source.type not in ("email",):
            return f"эта вакансия на сайте {source.name}"
        host = (urlparse(url).hostname or "").removeprefix("www.")
        known = {"linkedin.com": "LinkedIn", "hh.ru": "hh.ru", "superjob.ru": "SuperJob", "career.habr.com": "Хабр Карьера"}
        name = next((v for k, v in known.items() if host == k or host.endswith("." + k)), host or "источника")
        return f"эта вакансия на сайте {name}"

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
        label = getattr(labels, f"{'ru' if russian else 'en'}_{place}")
        return [label] if label else []  # an empty label in apply.yaml = leave «👾где» empty

    def manual(self, category: str) -> bool:
        rule = self._config.rules.categories.get(category)
        return bool(rule and rule.kind == "manual")

    def labels(self, category: str) -> list[str]:
        """«📋не IT» for categories worked by hand (kind: manual); label_retrieved if set (empty: the 🦆 in the name)."""
        obsidian = self._config.apply.obsidian
        return [label for label in (obsidian.label_retrieved, obsidian.label_non_it if self.manual(category)
                                    else "") if label]


@dataclass
class ApplyServices:
    """What the steps work with; shared by all applications."""

    builder: DossierBuilder
    vault: ObsidianVault | None
    drive: CloudFolders
    writer: MaterialsWriter
    skill: ResumeSkill
    out_dir: Path  # where the CV PDF goes (apply.yaml → resume.output_dir, ~/Downloads)
    folder_name: str = "{title} • {employer} • {date} 🦆"  # apply.yaml → drive.folder_name; date of ▶, yyyy-mm-dd


@dataclass
class StepContext:
    """One pass over one application."""

    vacancy_id: int
    services: ApplyServices
    warnings: list[str] = field(default_factory=list)
    kind: str = "apply"
    with_cv: bool = True

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


def wants_cv(app: Application) -> bool:
    """The CV (its PDF and the Drive folder): only for ▶ with that box checked at Generate."""
    return app.kind == APPLY and app.with_cv


def wants_letter(app: Application) -> bool:
    """The cover letter: ▶ with that box checked (for IT it comes only with the CV — it carries the CV's link)."""
    return app.kind == APPLY and app.with_letter


def wants_materials(app: Application) -> bool:
    """The one Claude call that picks the CV's blocks and / or writes the letter."""
    return wants_cv(app) or wants_letter(app)



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
        return {"note_path": str(ctx.vault.create(ctx.dossier, not_now=app.kind == NOT_NOW,
                                                   mine=wants_materials(app)))}


def drive_folder_name(template: str, title: str, employer: str, day: date) -> str:
    """«Data Analyst • Tribe • 2026-10-29 🦆»; no employer — «Data Analyst • 2026-10-29 🦆» (no dangling separator)."""
    if not employer:
        template = re.sub(r"\s*[•\-–—|]?\s*\{employer\}", "", template)
    name = template.format(title=title, employer=employer, date=day.isoformat())
    return " ".join(name.replace("/", "∕").split())


class FolderStep(Step):
    name = "folder"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return wants_cv(app) and ctx.drive.available and not app.folder_id and not ctx.dossier.manual

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        name = drive_folder_name(ctx.services.folder_name, ctx.dossier.name_title or ctx.dossier.title,
                                 ctx.dossier.employer, date.today())
        item = ctx.drive.ensure_folder(app.vacancy_id, name)
        return {"folder_id": item.id, "folder_url": item.url}


class MaterialsStep(Step):
    name = "materials"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return wants_materials(app) and not app.materials_json

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        if ctx.dossier.manual:
            materials = asyncio.run(ctx.writer.write_manual(ctx.dossier, app.id))
        else:
            materials = asyncio.run(ctx.writer.write(ctx.dossier, app.id, letter=wants_letter(app)))
        return {"materials_json": materials.to_json(), "skills": ", ".join(materials.skills)}


def downloads_path(folder: Path, name: str, own: str | None) -> Path:
    """`name` in the folder; taken by another file (not this application's own earlier PDF) → «name 2.pdf» …"""
    path, n = folder / name, 2
    stem, suffix = Path(name).stem, Path(name).suffix
    while path.exists() and str(path) != own:
        path, n = folder / f"{stem} {n}{suffix}", n + 1
    return path


class PdfStep(Step):
    name = "pdf"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return wants_cv(app) and not (app.pdf_path and Path(app.pdf_path).exists()) and not ctx.dossier.manual

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        """Built in a temporary folder; only the PDF is kept, in ~/Downloads (user, 30.10.2026: nothing in data/)."""
        assert app.materials is not None
        with tempfile.TemporaryDirectory(prefix="vr-cv-") as work:
            pdf, warning = ctx.skill.build(app.materials.selection, Path(work))
            target = downloads_path(ctx.out_dir, pdf.name, app.pdf_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pdf, target)
        if warning:
            log.warning("Application %s: %s", app.id, warning)
        return {"pdf_path": str(target), "pdf_file_id": None, "pdf_url": None}


class UploadStep(Step):
    name = "upload"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return (wants_cv(app) and ctx.drive.available and bool(app.folder_id) and not app.pdf_file_id
                and not ctx.dossier.manual)

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        assert app.folder_id and app.pdf_path
        item = ctx.drive.upload_pdf(app.vacancy_id, app.folder_id, Path(app.pdf_path))
        return {"pdf_file_id": item.id, "pdf_url": item.url}


class FinalizeStep(Step):
    """Always runs (cheap, idempotent): the letter gets the CV link, the note gets the letter, skills and folder."""

    name = "finalize"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return app.materials is not None if wants_materials(app) else bool(app.skills)

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        note = ctx.vault.find(app.vacancy_id, app.note_path) if ctx.vault else None
        if not wants_materials(app):  # Not now, or Generate with the note only: properties and skills, no «### моё»
            if note is not None and app.kind == APPLY:
                ctx.vault.become_applied(note, ctx.dossier.employer, mine=False)  # type: ignore[union-attr]
            if note is not None and app.skills:
                ctx.vault.fill_property(note, SKILLS_PROPERTY, app.skills)  # type: ignore[union-attr]
            return {"note_path": str(note)} if note else {}
        assert app.materials is not None
        letter = insert_cv_link(app.materials.letter, app.pdf_url) if wants_letter(app) else None
        updates: dict[str, Any] = {"letter": letter}
        if note is not None:
            ctx.vault.become_applied(note, ctx.dossier.employer)  # type: ignore[union-attr]  # was «не сейчас»
            mine = ctx.vault.mine_section(app.folder_url, letter,  # type: ignore[union-attr]
                                          folder=wants_cv(app) and not ctx.dossier.manual)
            if ctx.vault.set_mine(note, mine, app.note_mine):  # app.note_mine: what the app wrote there last time
                updates["note_mine"] = mine
            else:
                ctx.warnings.append("«### моё» in the note was edited by hand — not changed")
            if app.skills:
                ctx.vault.fill_property(note, SKILLS_PROPERTY, app.skills)  # type: ignore[union-attr]
            updates["note_path"] = str(note)
        elif ctx.vault is not None and app.note_path:
            ctx.warnings.append("The Obsidian note was not found (deleted?)")
        return updates


class SkillsStep(Step):
    """Not now, or Generate with the note only: the vacancy's skills for «👾необходимые скилы» (one small Claude call,
    no CV or letter)."""

    name = "skills"

    def needed(self, app: Application, ctx: StepContext) -> bool:
        return not wants_materials(app) and not app.skills

    def run(self, app: Application, ctx: StepContext) -> dict[str, Any]:
        return {"skills": ", ".join(asyncio.run(ctx.writer.skills(ctx.dossier, app.id)))}


STEPS: list[Step] = [NoteStep(), FolderStep(), MaterialsStep(), SkillsStep(), PdfStep(), UploadStep(), FinalizeStep()]


class ApplicationPipeline:
    """Runs the steps that are not done yet. Any stop (restart, limit, error) resumes at the first undone step."""

    def __init__(self, storage: Storage, services: ApplyServices, retry_minutes: int, steps: list[Step] | None = None):
        self._repo = storage.applications
        self._services = services
        self._retry = timedelta(minutes=retry_minutes)
        self._steps = steps or STEPS

    def process(self, app: Application) -> Application:
        ctx = StepContext(app.vacancy_id, self._services, kind=app.kind, with_cv=wants_cv(app))
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
            started = time.monotonic()
            try:
                updates = step.run(app, ctx)
            except ApplyError as exc:
                return self._stop(app, step, exc)
            except Exception as exc:  # one bad vacancy must not stop the worker
                log.exception("Application %s: step %s failed", app.id, step.name)
                return self._stop(app, step, ApplyError(f"{type(exc).__name__}: {exc}"))
            stats = app.stats
            stats.setdefault("steps", {})[step.name] = round(time.monotonic() - started, 2)
            self._repo.update(app.id, **updates, stats_json=json.dumps(stats))
        self._repo.update(app.id, state=ApplicationState.DONE, step=None, error=None,
                          warning="; ".join(self._warnings(ctx)) or None)
        return self._repo.get(app.id) or app

    def _warnings(self, ctx: StepContext) -> list[str]:
        warnings = list(ctx.warnings)
        if ctx.vault is None:
            warnings.append("Obsidian vault not found (config/apply.yaml → obsidian.vault_dir)")
        if (ctx.with_cv and not ctx.dossier.manual
                and (not ctx.drive.available or isinstance(ctx.drive, StubDrive))):
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
