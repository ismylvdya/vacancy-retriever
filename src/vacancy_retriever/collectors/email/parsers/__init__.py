"""Precise e-mail parsers, by name (the `parser:` field of a sender in config/mail.yaml).

A precise parser turns one letter into separate vacancies without the LLM having to split it,
which saves tokens. See _template.py and docs/HANDBOOK.md ("Точный парсер письма").

A parser has three possible answers:
- a list of ParsedVacancy — the letter holds vacancies;
- raise IgnoreLetter(reason) — a KNOWN non-vacancy template (newsletter, promo, profile reminder):
  no LLM, hidden with the reason, the letter is marked processed after the run;
- raise UnknownLetter(reason) (or return []) — a template the parser does not recognise: no LLM and the
  letter is left untouched (unread), so nothing important from that sender is ever marked read by mistake.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from email.utils import parseaddr

from bs4 import BeautifulSoup

from ..generic import _part_text, decoded_header


@dataclass
class ParsedVacancy:
    title: str
    url: str = ""
    company: str = ""
    location: str = ""
    text: str = ""
    # Stable id of the vacancy at its site ("hh:138458007"): the same vacancy in several letters is collected once.
    key: str = ""
    # Public page with the full description, fetched at the "details" stage (domain must be in detail_domains).
    detail_url: str = ""
    extra: dict[str, str] = field(default_factory=dict)


class IgnoreLetter(Exception):
    """A known template without vacancies. The message is the reason shown in Processed → Hidden."""


class UnknownLetter(Exception):
    """A template the parser does not know. The letter stays unread and is reported in Statistics."""


@dataclass
class DetailResult:
    text: str
    # Set when the page itself rules the vacancy out (recruitment closed, office in Russia…): dropped before the LLM.
    skip_reason: str | None = None


EmailParser = Callable[[Message], list[ParsedVacancy]]
DetailParser = Callable[[str], DetailResult | None]

PARSERS: dict[str, EmailParser] = {}
DETAIL_PARSERS: dict[str, DetailParser] = {}


def register_parser(name: str) -> Callable[[EmailParser], EmailParser]:
    def decorator(func: EmailParser) -> EmailParser:
        PARSERS[name] = func
        return func

    return decorator


def register_detail(name: str) -> Callable[[DetailParser], DetailParser]:
    """Page parser for `detail_url` of vacancies produced by the e-mail parser with the same name."""

    def decorator(func: DetailParser) -> DetailParser:
        DETAIL_PARSERS[name] = func
        return func

    return decorator


def load_parsers() -> None:
    import importlib
    import pkgutil

    for module in pkgutil.iter_modules(__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")


# ---------------------------------------------------------------- helpers for parsers

_INVISIBLE = re.compile(r"[͏​-‍⁠﻿⠀­]+")


def html_soup(message: Message) -> BeautifulSoup:
    return BeautifulSoup(_part_text(message, "html") or "", "lxml")


def sender_address(message: Message) -> str:
    return parseaddr(decoded_header(message, "From"))[1].lower()


def subject(message: Message) -> str:
    return decoded_header(message, "Subject")


def clean(text: str) -> str:
    """Strips the invisible padding characters newsletters put into preheaders, collapses spaces."""
    return " ".join(_INVISIBLE.sub(" ", text).split())


def is_bulk(message: Message) -> bool:
    """Mass mailing (has an unsubscribe header or link) — personal / transactional letters usually don't."""
    if message.get("List-Unsubscribe"):
        return True
    body = (_part_text(message, "html") or _part_text(message, "plain") or "").lower()
    return any(word in body for word in ("unsubscribe", "отписаться", "отписки", "odjavite", "отписка"))


_SERBIA = ("serbia", "srbija", "сербия", "србија", "belgrade", "beograd", "белград", "београд", "novi sad", "нови сад",
           "нови-сад", "niš", "ниш", "kragujevac", "subotica")
_REMOTE = ("удал", "дистанц", "remote", "любой")


def office_abroad(city: str, work_format: str) -> bool:
    """A fixed city outside Serbia and a format that is not remote (office / hybrid / not stated)."""
    city_l, format_l = city.lower(), work_format.lower()
    if not city_l or any(word in city_l for word in _REMOTE) or any(word in format_l for word in _REMOTE):
        return False
    return not any(word in city_l for word in _SERBIA)


def field_value(text: str, label: str) -> str:
    """'Город: Москва' → 'Москва' (first line starting with the label)."""
    for line in text.splitlines():
        if line.startswith(label):
            return line[len(label):].strip(" :")
    return ""
