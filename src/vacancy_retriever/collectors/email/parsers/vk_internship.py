"""VK Internship: «Новые вакансии на <дата>» (info@internship.vk.company).

The letter lists internships by section ("Разработка:", "Машинное обучение:", "Аналитика:"):
`- «Title» (https://internship.vk.company/vacancy/<id>/)`. The public page has the city, the format, the company
and the description. Most VK internships are in Moscow / St Petersburg and close fast, so the page alone decides
two cheap drops before the LLM: "Набор закрыт" and an office / hybrid format in a fixed Russian city.
"""

from __future__ import annotations

import re
from email.message import Message

from bs4 import BeautifulSoup

from . import DetailResult, ParsedVacancy, UnknownLetter, clean, html_soup, office_abroad, register_detail, register_parser

_VACANCY = re.compile(r"internship\.vk\.company/vacancy/(\d+)")
_SECTION = re.compile(r"^(.+):$")
# The application form (required fields are marked "*") and page footers end the description.
_END = ("*", "Подать заявку", "Похожие вакансии", "Другие вакансии", "Часто задаваемые вопросы")


@register_parser("vk_internship")
def parse(message: Message) -> list[ParsedVacancy]:
    soup = html_soup(message)
    vacancies: list[ParsedVacancy] = []
    seen: set[str] = set()
    section = ""
    for element in soup.find_all(string=True):
        line = clean(str(element))
        if not line:
            continue
        heading = _SECTION.match(line)
        if heading and not element.find_parent("a"):
            section = heading.group(1)
            continue
        link = element.find_parent("a", href=True)
        match = _VACANCY.search(link["href"]) if link else None
        if not match or match.group(1) in seen:
            continue
        job_id = match.group(1)
        seen.add(job_id)
        title = line.strip("«»\"")
        url = f"https://internship.vk.company/vacancy/{job_id}/"
        text = "\n".join(filter(None, [title, f"Направление: {section}" if section else "",
                                       "VK Internship — стажировка в VK / компаниях группы VK (Россия)"]))
        vacancies.append(ParsedVacancy(title=title, url=url, company="VK", text=text, key=f"vk:{job_id}",
                                       detail_url=url))
    if not vacancies:
        raise UnknownLetter("VK Internship letter without vacancies")
    return vacancies


def _value(lines: list[str], label: str) -> str:
    return lines[lines.index(label) + 1] if label in lines and lines.index(label) + 1 < len(lines) else ""


@register_detail("vk_internship")
def parse_detail(html: str) -> DetailResult | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    lines = [line for line in (clean(t) for t in soup.stripped_strings) if line]
    if "Город:" not in lines:
        return None
    city, work_format = _value(lines, "Город:"), _value(lines, "Формат работы:")
    start = max(0, lines.index("Город:") - 3)
    end = next((i for i, line in enumerate(lines) if i > start and line in _END), len(lines))
    text = "\n".join(lines[start:end])[:6000]
    skip = None
    if "Набор закрыт" in lines:
        skip = "Recruitment closed"
    elif office_abroad(city, work_format):
        skip = "Office outside Serbia"
    return DetailResult(text=text, skip_reason=skip)
