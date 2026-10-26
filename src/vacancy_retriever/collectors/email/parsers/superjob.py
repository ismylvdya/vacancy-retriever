"""SuperJob (nobody@host.superjob.ru): vacancy mailings and promos.

Links are /ml/r/<base64 of the real path>?i=<token>; the `i=` token logs into the account without a password,
so only the decoded path is kept. Vacancy pages are /vakansii/<slug>-<id>.html. SuperJob pages are not fetched
(captcha). Letters without vacancy links are known promos (resume services, webinars, birthday offers).
"""

from __future__ import annotations

import base64
import binascii
import re
from email.message import Message

from . import IgnoreLetter, ParsedVacancy, UnknownLetter, clean, html_soup, is_bulk, register_parser

_REDIRECT = re.compile(r"superjob\.ru/ml/r/([A-Za-z0-9_\-]+=*)")
_VACANCY = re.compile(r"/vakansii/[^?#\s]*?-(\d+)\.html")


def real_path(href: str) -> str:
    """/ml/r/<base64> → the decoded path or URL (without the login token); other links as is, query dropped."""
    match = _REDIRECT.search(href)
    if not match:
        return href.split("?", 1)[0]
    encoded = match.group(1)
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


@register_parser("superjob")
def parse(message: Message) -> list[ParsedVacancy]:
    vacancies: list[ParsedVacancy] = []
    seen: set[str] = set()
    for link in html_soup(message).find_all("a", href=True):
        path = real_path(link["href"])
        match = _VACANCY.search(path)
        title = clean(link.get_text(" "))
        if not match or not title or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        url = "https://www.superjob.ru" + path[path.index("/vakansii/"):].split("?", 1)[0]
        block = link.find_parent(["tr", "td", "div"]) or link
        lines = [line for line in (clean(t) for t in block.stripped_strings) if line and line != title]
        text = "\n".join([title, *lines[:4], "SuperJob — подборка вакансий (Россия)"])
        vacancies.append(ParsedVacancy(title=title, url=url, text=text, key=f"superjob:{match.group(1)}"))
    if vacancies:
        return vacancies
    if is_bulk(message):
        raise IgnoreLetter("SuperJob promo without vacancies")
    raise UnknownLetter("SuperJob letter without vacancies")
