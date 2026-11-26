"""hh.ru (noreply@hh.ru): saved-search ("автопоиск") mailings and "Подходящие вакансии для резюме".

Since 16.11.2026 the user looks for work in Russia, office in Moscow included, so the resume recommendations are read
too (until then they were skipped: office or remote was unknown). Hints for the LLM: work_format=remote when the
letter says remote work; work_format=office for a row whose address has a metro station («Москва, м. Арбатская»).
The name of a saved search no longer means remote: «для 🦆 vacancy-retriever (…)» brought Moscow offices as well.

Every vacancy is a table row: title link to hh.ru/vacancy/<id>, optional salary, company, "Посмотреть вакансию".
Links in these letters carry `loginkey=` — a token that logs into the user's hh account. It must never be stored,
shown or opened, so only the clean https://hh.ru/vacancy/<id> is kept. The detail URL points to a public copy
on Get-Works; hh vacancies are in Russia.
"""

from __future__ import annotations

import re
from email.message import Message

from ...hh_descriptions import hh_description_url
from . import IgnoreLetter, ParsedVacancy, UnknownLetter, clean, html_soup, register_parser, subject

_VACANCY = re.compile(r"hh\.ru/vacancy/(\d+)")
_SALARY = re.compile(r"^(от|до)$|^[\d\s\u00a0\u202f]+$|₽|руб\.|\$|€|\bUSD\b|\bEUR\b|\bKZT\b|₸|за\s+месяц", re.I)
_BUTTONS = {"Посмотреть вакансию", "Откликнуться"}
_RESUME_PICKS = re.compile(r"подходящие вакансии для резюме", re.I)
_REMOTE = re.compile(r"удал[её]нн?(ая|о)\s+работ|удал[её]нно|удал[её]нка", re.I)
_METRO = re.compile(r"(?:^|,\s*)м\.\s")  # «Москва, м. Арбатская»: an address in the city, not remote


def _row_lines(link) -> list[str]:
    """Lines of the smallest element holding this vacancy and no other."""
    job_id = _VACANCY.search(link["href"]).group(1)
    block = link
    while block.parent is not None and set(_VACANCY.findall(str(block.parent))) <= {job_id}:
        block = block.parent
    return [line for line in (clean(t) for t in block.stripped_strings) if line and line not in _BUTTONS]


@register_parser("hh_ru")
def parse(message: Message) -> list[ParsedVacancy]:
    picks = bool(_RESUME_PICKS.search(subject(message)))
    soup = html_soup(message)
    remote = bool(_REMOTE.search(soup.get_text(" ")))
    seen: set[str] = set()
    vacancies: list[ParsedVacancy] = []
    for link in soup.find_all("a", href=True):
        match = _VACANCY.search(link["href"])
        title = clean(link.get_text(" "))
        if not match or not title or title in _BUTTONS or match.group(1) in seen:
            continue
        job_id = match.group(1)
        seen.add(job_id)
        lines = _row_lines(link)
        rest = lines[lines.index(title) + 1:] if title in lines else lines
        salary_parts = [line for line in rest if _SALARY.search(line)]
        salary = " ".join(salary_parts)
        place = next((line for line in rest if line not in salary_parts), "")
        # the real saved-search letter (28.10.2026) has «Работодатель, Город, м. Метро» in one line
        company, _, location = place.partition(", ")
        url = f"https://hh.ru/vacancy/{job_id}"
        origin = "hh.ru — подходящие вакансии для резюме" if picks else "hh.ru — автопоиск (вакансии по подписке)"
        work_format = "remote" if remote else "office" if _METRO.search(location) else None
        text = "\n".join(filter(None, [title, company, location, f"Зарплата: {salary}" if salary else "", origin]))
        vacancies.append(ParsedVacancy(title=title, url=url, company=company, location=location, text=text,
                                       key=f"hh:{job_id}", detail_url=hh_description_url(url),
                                       extra={"template": "hh_resume_picks" if picks else "hh_saved_search",
                                              **({"salary": salary} if salary else {}),
                                              **({"work_format": work_format} if work_format else {})}))
    if not vacancies:
        raise UnknownLetter("hh.ru letter without vacancy list")
    return vacancies
