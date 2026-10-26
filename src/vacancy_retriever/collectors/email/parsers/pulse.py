"""HR-платформа «Пульс» (pulse@mtsbank.ru and other banks on the same platform): «Вас порекомендовали на вакансию
«<title>» компании <company>». One personal recommendation = one vacancy. The letter body (name, phone, e-mail)
never goes to the LLM — only the title and the company; the link to the candidate page stays on the card."""

from __future__ import annotations

import re
from email.message import Message

from . import ParsedVacancy, UnknownLetter, html_soup, register_parser, subject

_SUBJECT = re.compile(r"вакансию\s*«(?P<title>[^»]+)»\s*компании\s*(?P<company>.+)$", re.I)


@register_parser("pulse")
def parse(message: Message) -> list[ParsedVacancy]:
    match = _SUBJECT.search(subject(message))
    if not match:
        raise UnknownLetter("«Пульс» letter that is not a recommendation")
    title, company = match.group("title").strip(), match.group("company").strip().strip(".")
    link = next((a["href"] for a in html_soup(message).find_all("a", href=True) if "/platform/candidate/" in a["href"]), "")
    text = f"{title}\n{company}\nЛичная рекомендация через HR-платформу «Пульс» (Россия)"
    return [ParsedVacancy(title=title, url=link.split("?", 1)[0], company=company, text=text,
                          key=f"pulse:{company}:{title}".lower())]
