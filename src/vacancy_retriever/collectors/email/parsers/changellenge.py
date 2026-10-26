"""Changellenge: career digests and promos (info@changellenge.com).

Digests mix events (case cups, hackathons, open days) with a few vacancies; every link goes through the
mckx.ru click tracker, the real address is in its `u=` parameter. Only links to changellenge.com/vacancy/... are
vacancies; their public page is parsed like the Changellenge site source. Letters with no vacancy at all are
known promos (resume builder, games, surveys).
"""

from __future__ import annotations

import re
from email.message import Message
from urllib.parse import parse_qs, urlparse

from . import (DetailResult, IgnoreLetter, ParsedVacancy, UnknownLetter, clean, field_value, html_soup, is_bulk,
               office_abroad, register_detail, register_parser)

_VACANCY = re.compile(r"^https://changellenge\.com/vacancy/([^/?#]+)/?")


def real_url(href: str) -> str:
    """mckx.ru tracker → the address it redirects to; any other link as is."""
    target = parse_qs(urlparse(href).query).get("u", [""])[0]
    return target or href


@register_parser("changellenge")
def parse(message: Message) -> list[ParsedVacancy]:
    vacancies: list[ParsedVacancy] = []
    seen: set[str] = set()
    for link in html_soup(message).find_all("a", href=True):
        match = _VACANCY.match(real_url(link["href"]))
        title = clean(link.get_text(" "))
        if not match or not title or match.group(1) in seen:
            continue
        slug = match.group(1)
        seen.add(slug)
        url = f"https://changellenge.com/vacancy/{slug}/"
        vacancies.append(ParsedVacancy(title=title, url=url, text=f"{title}\nChangellenge — карьерный дайджест",
                                       key=f"changellenge:{slug}", detail_url=url))
    if vacancies:
        return vacancies
    if is_bulk(message):
        raise IgnoreLetter("Changellenge newsletter without vacancies")
    raise UnknownLetter("Changellenge letter without vacancies")


@register_detail("changellenge")
def parse_detail(html: str) -> DetailResult | None:
    from ...sites.changellenge import parse_detail as site_detail

    text = site_detail(html)
    if not text:
        return None
    abroad = office_abroad(field_value(text, "Город:"), field_value(text, "Формат:"))
    return DetailResult(text=text, skip_reason="Office outside Serbia" if abroad else None)
