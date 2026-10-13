from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError
from ..registry import register
from ._common import ListingCrawler, card_text, html_to_text

HABR_BASE = "https://career.habr.com"
API_URL = f"{HABR_BASE}/api/frontend/vacancies"
DATA_ANALYST, DATA_SCIENTIST, DATA_ENGINEER, ML_ENGINEER, MLOPS = 43, 44, 76, 125, 129
INTERN, JUNIOR = 1, 3
DEFAULT_PARAMS: dict[str, Any] = {
    "specializations": [DATA_ANALYST, DATA_SCIENTIST, DATA_ENGINEER, ML_ENGINEER, MLOPS],
    "qualifications": [INTERN, JUNIOR],
    "remote_only": False,
}


def parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    published = datetime.fromisoformat(value)
    return published.replace(tzinfo=UTC) if published.tzinfo is None else published.astimezone(UTC)


def _titles(entries: list[dict[str, Any]] | None) -> list[str]:
    return [e["title"] for e in entries or [] if e.get("title")]


def vacancy_salary(vacancy: dict[str, Any]) -> str | None:
    salary = (vacancy.get("salary") or {}).get("formatted")
    if salary:
        return salary
    predicted = (vacancy.get("predictedSalary") or {}).get("formatted")
    return f"{predicted} (прогноз Хабра)" if predicted else None


def vacancy_text(vacancy: dict[str, Any]) -> str:
    company = (vacancy.get("company") or {}).get("title")
    locations = _titles(vacancy.get("locations"))
    salary = vacancy_salary(vacancy)
    grade = (vacancy.get("salaryQualification") or {}).get("title") or vacancy.get("qualification")
    divisions = _titles(vacancy.get("divisions"))
    skills = _titles(vacancy.get("skills"))
    return card_text(
        vacancy.get("title"),
        f"Компания: {company}" if company else None,
        f"Локация: {', '.join(locations)}" if locations else None,
        "Можно удалённо" if vacancy.get("remoteWork") else None,
        f"Зарплата: {salary}" if salary else None,
        f"Грейд: {grade}" if grade else None,
        f"Специализация: {', '.join(divisions)}" if divisions else None,
        f"Навыки: {', '.join(skills)}" if skills else None,
        f"Занятость: {vacancy['employment']}" if vacancy.get("employment") else None,
    )


def vacancy_item(vacancy: dict[str, Any], source_id: str = "") -> RawItem:
    vacancy_id = str(vacancy["id"])
    url = HABR_BASE + (vacancy.get("href") or f"/vacancies/{vacancy_id}")
    locations = _titles(vacancy.get("locations")) + (["удалённо"] if vacancy.get("remoteWork") else [])
    return RawItem(
        source_id=source_id,
        external_id=vacancy_id,
        url=url,
        title=vacancy.get("title") or "",
        text=vacancy_text(vacancy),
        published_at=parse_published((vacancy.get("publishedDate") or {}).get("date")),
        detail_url=url,
        meta={
            "company": (vacancy.get("company") or {}).get("title"),
            "location": ", ".join(locations) or None,
            "grade": vacancy.get("qualification"),
        },
    )


def parse_vacancies(data: dict[str, Any], source_id: str = "") -> list[RawItem]:
    return [vacancy_item(v, source_id) for v in data.get("list") or [] if v.get("id")]


def parse_vacancy_page(html: str) -> str:
    description = BeautifulSoup(html, "lxml").select_one(".vacancy-description__text")
    if description is None:
        raise SourceError("career.habr.com: vacancy page has no description block (layout changed?)")
    return html_to_text(description)


@register
class HabrCollector(Collector):
    """Habr Career frontend JSON API (sorted by date, page paging); details are parsed from the vacancy page."""

    type_name = "habr"

    def params(self) -> dict[str, Any]:
        return {**DEFAULT_PARAMS, **self.source.params}

    def query(self, page: int) -> dict[str, object]:
        params = self.params()
        query: dict[str, object] = {"sort": "date", "type": "all", "page": page}
        if params["specializations"]:
            query["s[]"] = params["specializations"]
        if params["qualifications"]:
            query["qid[]"] = params["qualifications"]
        if params["remote_only"]:
            query["remote"] = "true"
        return query

    def collect(self, ctx: CollectContext) -> CollectResult:
        crawler = ListingCrawler(ctx)
        crawler.crawl(self._fetch_page)
        return crawler.result()

    def _fetch_page(self, page: int) -> tuple[list[RawItem], bool]:
        data = self.http.get_json(self.source.url or API_URL, **self.query(page))
        total_pages = (data.get("meta") or {}).get("totalPages", 0)
        return parse_vacancies(data, self.source.id), page < total_pages

    def fetch_detail(self, item: RawItem) -> str | None:
        if not item.detail_url:
            return None
        return parse_vacancy_page(self.http.get_text(item.detail_url))

    def icon_url(self) -> str | None:
        return f"{HABR_BASE}/images/favicons/favicon-32.png"
