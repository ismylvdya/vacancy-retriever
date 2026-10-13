from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .items import RawItemRepository
from .llm_calls import LlmCallRepository
from .runs import RunRepository
from .vacancies import VacancyRepository


@dataclass
class Storage:
    db: Database
    runs: RunRepository
    items: RawItemRepository
    vacancies: VacancyRepository
    llm_calls: LlmCallRepository

    @classmethod
    def open(cls, path: Path, token_log: Path | None = None) -> Storage:
        db = Database(path)
        return cls(db, RunRepository(db), RawItemRepository(db), VacancyRepository(db), LlmCallRepository(db, token_log))
