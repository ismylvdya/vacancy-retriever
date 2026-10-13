"""Precise e-mail parsers, by name (the `parser:` field of a sender in config/mail.yaml).

A precise parser turns one letter into separate vacancies without the LLM having to split it,
which saves tokens. See _template.py and docs/HANDBOOK.md ("Точный парсер письма").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message


@dataclass
class ParsedVacancy:
    title: str
    url: str = ""
    company: str = ""
    location: str = ""
    text: str = ""
    extra: dict[str, str] = field(default_factory=dict)


EmailParser = Callable[[Message], list[ParsedVacancy]]

PARSERS: dict[str, EmailParser] = {}


def register_parser(name: str) -> Callable[[EmailParser], EmailParser]:
    def decorator(func: EmailParser) -> EmailParser:
        PARSERS[name] = func
        return func

    return decorator


def load_parsers() -> None:
    import importlib
    import pkgutil

    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")
