"""hh.ru (noreply@hh.ru): saved-search ("автопоиск") mailings; "Подходящие вакансии для резюме" are skipped.

The resume recommendations do not say whether a job is remote and hh pages cannot be fetched, so most of them would
be Moscow offices shown as remote work (decision 2026-10-26): they are a known non-vacancy template. The user keeps
saved searches with the "Удалённая работа" filter instead; their vacancies get the hint work_format=remote when the
letter mentions remote work or the saved search is named "… vacancy-retriever …".

Every vacancy is a table row: title link to hh.ru/vacancy/<id>, optional salary, company, "Посмотреть вакансию".
Links in these letters carry `loginkey=` — a token that logs into the user's hh account. It must never be stored,
shown or opened, so only the clean https://hh.ru/vacancy/<id> is kept. hh pages are not fetched (the site
answers 406 to non-browser clients) — the LLM gets what the letter says; hh vacancies are in Russia.
"""

from __future__ import annotations

import re
from email.message import Message

from . import IgnoreLetter, ParsedVacancy, UnknownLetter, clean, html_soup, register_parser, subject

_VACANCY = re.compile(r"hh\.ru/vacancy/(\d+)")
_SALARY = re.compile(r"^(от|до)$|^[\d\s\u00a0\u202f]+$|₽|руб\.|\$|€|\bUSD\b|\bEUR\b|\bKZT\b|₸|за\s+месяц", re.I)
_BUTTONS = {"Посмотреть вакансию", "Откликнуться"}
_RESUME_PICKS = re.compile(r"подходящие вакансии для резюме", re.I)
# Remote work is either stated in the letter, or the saved search carries the "vacancy-retriever" mark in its name:
# the user names remote-only saved searches "для 🦆 vacancy-retriever (…)" (2026-10-26).
_REMOTE = re.compile(r"удал[её]нн?(ая|о)\s+работ|удал[её]нно|удал[её]нка|vacancy-retriever", re.I)


def _row_lines(link) -> list[str]:
    """Lines of the smallest element holding this vacancy and no other."""
    job_id = _VACANCY.search(link["href"]).group(1)
    block = link
    while block.parent is not None and set(_VACANCY.findall(str(block.parent))) <= {job_id}:
        block = block.parent
    return [line for line in (clean(t) for t in block.stripped_strings) if line and line not in _BUTTONS]


@register_parser("hh_ru")
def parse(message: Message) -> list[ParsedVacancy]:
    if _RESUME_PICKS.search(subject(message)):
        raise IgnoreLetter("hh.ru resume recommendations (office / remote unknown)")
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
        text = "\n".join(filter(None, [title, company, location, f"Зарплата: {salary}" if salary else "",
                                       "hh.ru — автопоиск (вакансии по подписке)"]))
        vacancies.append(ParsedVacancy(title=title, url=url, company=company, location=location, text=text,
                                       key=f"hh:{job_id}",
                                       extra={"template": "hh_saved_search", **({"salary": salary} if salary else {}),
                                              **({"work_format": "remote"} if remote else {})}))
    if not vacancies:
        raise UnknownLetter("hh.ru letter without vacancy list")
    return vacancies
