from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

import yaml

from ..config import ObsidianSettings
from .models import VacancyDossier

ID_PROPERTY = "retriever_id"
SKILLS_PROPERTY = "👾необходимые скилы"
_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_LEGAL = re.compile(r"\b(d\.?o\.?o\.?|llc|ltd|inc|gmbh|ооо|ао|пао|ип)\b|\([^)]*\)", re.I)
_PLACEHOLDER = "_готовится…_"
DESCRIPTION_HEADING = "### описание вакансии"
MINE_HEADING = "### моё"
_MINE = re.compile(rf"^{re.escape(MINE_HEADING)}[ \t]*\n(.*?)(?=^#{{1,3}} |\Z)", re.S | re.M)


def note_file_name(title: str, employer: str) -> str:
    """«<работодатель> • <вакансия>» (user, 20.11.2026; before: vacancy first); the app's 🦆 is added by
    ObsidianVault.create. Notes made earlier keep their names (found by retriever_id)."""
    raw = f"{employer} • {title}" if employer else title
    name = " ".join(_FORBIDDEN.sub(" ", raw.strip()).split()).lstrip(".")
    return (name[:120].rstrip() or "Vacancy")


def normalize_company(name: str) -> str:
    return " ".join(re.sub(r"[^\w ]+", " ", _LEGAL.sub(" ", name.casefold())).split())


def _label(text: str) -> str:
    return text.replace("[", "(").replace("]", ")")


def _fence(text: str) -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text.strip()}\n{ticks}"


def _yaml_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    try:
        data = yaml.safe_load(parts[1]) if len(parts) == 3 else None
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class NoteRenderer:
    settings: ObsidianSettings

    def render(self, dossier: VacancyDossier, employer_link: str | None, today: date | None = None,
               not_now: bool = False, mine: bool = True) -> str:
        """Properties like the user's own vacancy notes (начало = today, статус, one «где»), then the template
        «### описание вакансии» (sources + text) and «### моё» (Drive folder + letter, filled in when ready)."""
        started = (today or date.today()).isoformat()
        status = self.settings.status_not_now if not_now else self.settings.status
        if not_now:
            employer_link = None  # Not now: «👾FK» stays empty (the user's template, 30.10.2026)
        lines = ["---", "tags:", *[f"  - {_yaml_value(t)}" for t in self.settings.tags],
                 "👾что_это:", f"  - {_yaml_value(self.settings.what)}", f"начало: {started}", "👾статус:",
                 *([f"  - {_yaml_value(status)}"] if status else []), "👾где:",
                 *[f"  - {_yaml_value(w)}" for w in dossier.where[:1]], "👾лэйблы:",
                 *[f"  - {_yaml_value(label)}" for label in dossier.labels],
                 f"👾FK: {_yaml_value(employer_link) if employer_link else ''}".rstrip(),
                 f"{SKILLS_PROPERTY}:", f"{ID_PROPERTY}: {dossier.vacancy_id}", "---", ""]
        sources = [f"[{_label(label)}]({url})" for label, url in dossier.links]
        body = [DESCRIPTION_HEADING, *sources, "", "**описание вакансии:**", _fence(dossier.text), ""]
        if mine and not not_now:  # «### моё» holds the Drive folder and the letter: only when they are made
            body += [MINE_HEADING, _PLACEHOLDER, ""]
        return "\n".join(lines + body)


class ObsidianVault:
    """Vacancy notes in the user's vault. A fresh generation can replace its existing note."""

    def __init__(self, root: Path, settings: ObsidianSettings):
        self.root = root
        self.settings = settings
        self.renderer = NoteRenderer(settings)

    @property
    def available(self) -> bool:
        return self.root.is_dir()

    def open_url(self, path: Path) -> str:
        relative = path.relative_to(self.root).with_suffix("").as_posix()
        return f"obsidian://open?vault={quote(self.root.name)}&file={quote(relative)}"

    def _notes(self) -> Iterator[Path]:
        """Markdown notes of the vault, without its service folders (.obsidian, .trash)."""
        for path in self.root.rglob("*.md"):
            if not any(part.startswith(".") for part in path.relative_to(self.root).parts):
                yield path

    def find(self, vacancy_id: int, known: str | None = None) -> Path | None:
        """The note of this vacancy: the stored path, else any note with its retriever_id (the user renamed it)."""
        if known and Path(known).is_file():
            return Path(known)
        for path in self._notes():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if f"{ID_PROPERTY}:" in text and str(frontmatter(text).get(ID_PROPERTY)) == str(vacancy_id):
                return path
        return None

    def create(self, dossier: VacancyDossier, not_now: bool = False, mine: bool = True) -> Path:
        existing = self.find(dossier.vacancy_id)
        content = self.renderer.render(dossier, self.employer_link(dossier.employer), not_now=not_now, mine=mine)
        if existing:
            with TemporaryDirectory(prefix=".vr-note-", dir=existing.parent) as work:
                replacement = Path(work) / existing.name
                replacement.write_text(content, encoding="utf-8")
                replacement.replace(existing)
            return existing
        folder = self.root / self.settings.folder if self.settings.folder else self.root
        folder.mkdir(parents=True, exist_ok=True)
        base = note_file_name(dossier.name_title or dossier.title, dossier.employer)
        mark = f" {self.settings.app_mark}" if self.settings.app_mark else ""
        path, n = folder / f"{base}{mark}.md", 2
        while True:
            try:
                with path.open("x", encoding="utf-8") as note:
                    note.write(content)
                return path
            except FileExistsError:
                path, n = folder / f"{base} {n}{mark}.md", n + 1

    def employer_link(self, employer: str) -> str | None:
        wanted = normalize_company(employer)
        if not wanted:
            return None
        for path in self._notes():
            meta = frontmatter(path.read_text(encoding="utf-8", errors="ignore")[:4000])
            what = meta.get("👾что_это") or []
            if self.settings.employer_what not in (what if isinstance(what, list) else [what]):
                continue
            aliases = meta.get("aliases") or []
            names = [path.stem, *(aliases if isinstance(aliases, list) else [aliases])]
            if any(normalize_company(str(name)) == wanted for name in names):
                return f"[[{path.stem}]]"
        return None

    def become_applied(self, path: Path, employer: str, mine: bool = True) -> None:
        """A «не сейчас» note, then ▶: the status turns «откликнулся» (only if it is still the app's «не сейчас»),
        the empty «👾FK» gets the employer and the «### моё» section is added for the letter."""
        text = path.read_text(encoding="utf-8")
        old = f"👾статус:\n  - {_yaml_value(self.settings.status_not_now)}\n"
        if self.settings.status and old in text:
            text = text.replace(old, f"👾статус:\n  - {_yaml_value(self.settings.status)}\n", 1)
        if mine and not _MINE.search(text):
            text = text.rstrip() + f"\n\n{MINE_HEADING}\n{_PLACEHOLDER}\n"
        path.write_text(text, encoding="utf-8")
        link = self.employer_link(employer)
        if link:
            self.fill_property(path, "👾FK", link)

    @staticmethod
    def set_mine(path: Path, content: str, previous: str | None) -> bool:
        """Fills «### моё». It is rewritten only while it holds the placeholder or exactly what the app wrote there
        last time (`previous`): once the user edits the section — or removes the heading — it is left alone."""
        text = path.read_text(encoding="utf-8")
        match = _MINE.search(text)
        if not match:
            return False
        current = match.group(1).strip()
        if current not in {_PLACEHOLDER, (previous or "").strip()} or current == content.strip():
            return current == content.strip()
        new = f"{text[:match.start(1)]}{content.strip()}\n\n{text[match.end(1):].lstrip()}".rstrip() + "\n"
        path.write_text(new, encoding="utf-8")
        return True

    @staticmethod
    def fill_property(path: Path, key: str, value: str) -> bool:
        """Sets `key: value` only when the property exists and is empty (a value the user typed is kept)."""
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return False
        _, head, rest = text.split("---", 2)
        pattern = re.compile(rf"^{re.escape(key)}:[ \t]*$(?!\n[ \t]+-)", re.M)
        if not pattern.search(head):
            return False
        head = pattern.sub(lambda _: f"{key}: {_yaml_value(value)}", head, count=1)
        path.write_text(f"---{head}---{rest}", encoding="utf-8")
        return True

    @staticmethod
    def mine_section(folder_url: str | None, letter: str | None, folder: bool = True) -> str:
        """«### моё»: the Drive folder (when a CV was made), then the letter (with the link to the CV file) in a code
        block (when one was asked for). Hands-on work has no CV and no folder: only the letter."""
        parts = []
        if folder:
            parts.append(f"[папка на Google Drive]({folder_url})" if folder_url else "_Google Drive не подключён — папки нет._")
        if letter:
            parts.append(f"**cover letter:**\n{_fence(letter)}")
        return "\n\n".join(parts)
