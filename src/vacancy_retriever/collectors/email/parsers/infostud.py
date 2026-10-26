"""Poslovi Infostud (noreply@infostudmail.com): job alerts and account reminders.

Vacancies are links to poslovi.infostud.com/posao/<slug>/<id>; their page is parsed like the infostud site source.
Letters without vacancy links are known reminders ("Vaš profil je skoro gotov", newsletters)."""

from __future__ import annotations

import re
from email.message import Message

from . import DetailResult, IgnoreLetter, ParsedVacancy, UnknownLetter, clean, html_soup, is_bulk, register_detail, register_parser

_VACANCY = re.compile(r"https?://poslovi\.infostud\.com/posao/[^?#\s\"']*?(\d{5,})")


@register_parser("infostud")
def parse(message: Message) -> list[ParsedVacancy]:
    vacancies: list[ParsedVacancy] = []
    seen: set[str] = set()
    for link in html_soup(message).find_all("a", href=True):
        match = _VACANCY.search(link["href"])
        title = clean(link.get_text(" "))
        if not match or not title or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        url = match.group(0)
        block = link.find_parent(["tr", "td", "div"]) or link
        lines = [line for line in (clean(t) for t in block.stripped_strings) if line and line != title]
        vacancies.append(ParsedVacancy(title=title, url=url, text="\n".join([title, *lines[:4]]),
                                       key=f"infostud:{match.group(1)}", detail_url=url))
    if vacancies:
        return vacancies
    if is_bulk(message):
        raise IgnoreLetter("Infostud reminder / newsletter")
    raise UnknownLetter("Infostud letter without vacancies")


@register_detail("infostud")
def parse_detail(html: str) -> DetailResult | None:
    from ...sites.infostud import parse_detail as site_detail

    text = site_detail(html)
    return DetailResult(text=text) if text else None
