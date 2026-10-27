from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import yaml

from ..config import ObsidianSettings
from .models import VacancyDossier

ID_PROPERTY = "retriever_id"
SKILLS_PROPERTY = "👾необходимые скилы"
_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_LEGAL = re.compile(r"\b(d\.?o\.?o\.?|llc|ltd|inc|gmbh|ооо|ао|пао|ип)\b|\([^)]*\)", re.I)
_PLACEHOLDER = "_готовится…_"


def note_file_name(title: str, employer: str) -> str:
    name = " ".join(_FORBIDDEN.sub(" ", f"{title} {employer}".strip()).split()).lstrip(".")
    return (name[:120].rstrip() or "Vacancy")


def normalize_company(name: str) -> str:
    return " ".join(re.sub(r"[^\w ]+", " ", _LEGAL.sub(" ", name.casefold())).split())


def _fence(text: str) -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text.strip()}\n{ticks}"


def _yaml_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _block(name: str, content: str) -> str:
    return f"%%vr:{name}%%\n{content.strip()}\n%%/vr:{name}%%"


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

    def render(self, dossier: VacancyDossier, employer_link: str | None) -> str:
        lines = ["---", "tags:", *[f"  - {_yaml_value(t)}" for t in self.settings.tags],
                 "👾что_это:", f"  - {_yaml_value(self.settings.what)}", "👾где:",
                 *[f"  - {_yaml_value(w)}" for w in dossier.where], "👾лэйблы:",
                 *[f"  - {_yaml_value(label)}" for label in dossier.labels],
                 f"👾FK: {_yaml_value(employer_link) if employer_link else ''}".rstrip(),
                 f"{SKILLS_PROPERTY}:", "👾статус:", "начало:", f"{ID_PROPERTY}: {dossier.vacancy_id}", "---", ""]
        body = [*dossier.links, "",
                "## Резюме", _block("folder", _PLACEHOLDER), "",
                "## Сопроводительное", _block("letter", _PLACEHOLDER), "",
                "## Описание", _fence(dossier.text), ""]
        return "\n".join(lines + body)


class ObsidianVault:
    """Vacancy notes in the user's vault. Only the app's own blocks and empty properties are ever changed."""

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
        marker = re.compile(rf"^{ID_PROPERTY}:\s*{vacancy_id}\s*$", re.M)
        for path in self._notes():
            head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
            if head.startswith("---") and marker.search(head):
                return path
        return None

    def create(self, dossier: VacancyDossier) -> Path:
        existing = self.find(dossier.vacancy_id)
        if existing:
            return existing
        folder = self.root / self.settings.folder if self.settings.folder else self.root
        folder.mkdir(parents=True, exist_ok=True)
        base = note_file_name(dossier.title, dossier.employer)
        path, n = folder / f"{base}.md", 2
        while path.exists():
            path, n = folder / f"{base} ({n}).md", n + 1
        path.write_text(self.renderer.render(dossier, self.employer_link(dossier.employer)), encoding="utf-8")
        return path

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

    @staticmethod
    def set_block(path: Path, name: str, content: str) -> bool:
        """Replaces the text between %%vr:name%% and %%/vr:name%%. No markers (the user removed them) — no change."""
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(rf"%%vr:{re.escape(name)}%%\n.*?%%/vr:{re.escape(name)}%%", re.S)
        if not pattern.search(text):
            return False
        path.write_text(pattern.sub(lambda _: _block(name, content), text, count=1), encoding="utf-8")
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
    def letter_block(letter: str) -> str:
        return _fence(letter)

    @staticmethod
    def folder_block(folder_url: str | None, pdf_url: str | None) -> str:
        if not folder_url:
            return "_Google Drive не подключён — папки нет._"
        line = f"[Папка на Google Drive]({folder_url})"
        return f"{line} · [PDF]({pdf_url})" if pdf_url else line
