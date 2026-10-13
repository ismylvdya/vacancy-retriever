from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError
from ..registry import register
from ._common import ListingCrawler, card_text, html_to_text

GEEKJOB_BASE = "https://geekjob.ru"
API_URL = f"{GEEKJOB_BASE}/json/find/vacancy"
DATA_SCIENCE_GROUP = 52
DEFAULT_PARAMS: dict[str, Any] = {"tags": [DATA_SCIENCE_GROUP], "remote": True, "relocate": False}

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_RU_DATE = re.compile(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?", re.I)
_FORMATS = {"remote": "удалённо", "relocate": "релокация", "inhouse": "офис", "parttime": "частичная занятость"}


def parse_ru_date(value: str | None, now: datetime) -> datetime | None:
    """`7 октября` / `18 мая 2025` / `сегодня` / `вчера` → midnight UTC; a yearless date in the future is last year's."""
    text = (value or "").strip().lower()
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "сегодня":
        return today
    if text == "вчера":
        return today - timedelta(days=1)
    match = _RU_DATE.search(text)
    if not match or match.group(2) not in _MONTHS:
        return None
    day, month = int(match.group(1)), _MONTHS[match.group(2)]
    year = int(match.group(3)) if match.group(3) else today.year
    published = datetime(year, month, day, tzinfo=UTC)
    if not match.group(3) and published > today + timedelta(days=1):
        published = published.replace(year=year - 1)
    return published


def vacancy_url(vacancy_id: str) -> str:
    return f"{GEEKJOB_BASE}/vacancy/{vacancy_id}"


def vacancy_location(vacancy: dict[str, Any]) -> str:
    return ", ".join(part.strip() for part in (vacancy.get("city"), vacancy.get("country")) if part and part.strip())


def vacancy_formats(vacancy: dict[str, Any]) -> list[str]:
    job_format = vacancy.get("jobFormat") or {}
    return [label for key, label in _FORMATS.items() if job_format.get(key)]


def vacancy_text(vacancy: dict[str, Any]) -> str:
    company = (vacancy.get("company") or {}).get("name")
    location = vacancy_location(vacancy)
    formats = vacancy_formats(vacancy)
    return card_text(
        (vacancy.get("position") or "").strip(),
        f"Компания: {company}" if company else None,
        f"Локация: {location}" if location else None,
        f"Формат: {', '.join(formats)}" if formats else None,
        f"Зарплата: {vacancy['salary']}" if vacancy.get("salary") else None,
    )


def vacancy_item(vacancy: dict[str, Any], now: datetime, source_id: str = "") -> RawItem:
    vacancy_id = str(vacancy["id"])
    url = vacancy_url(vacancy_id)
    location = ", ".join(filter(None, [vacancy_location(vacancy), *vacancy_formats(vacancy)[:2]]))
    return RawItem(
        source_id=source_id,
        external_id=vacancy_id,
        url=url,
        title=(vacancy.get("position") or "").strip(),
        text=vacancy_text(vacancy),
        published_at=parse_ru_date((vacancy.get("log") or {}).get("modify"), now),
        detail_url=url,
        meta={"company": (vacancy.get("company") or {}).get("name"), "location": location or None},
    )


def parse_vacancies(data: dict[str, Any], now: datetime, source_id: str = "") -> list[RawItem]:
    return [vacancy_item(v, now, source_id) for v in data.get("data") or [] if v.get("id")]


def _node_text(node: Tag | None) -> str | None:
    return html_to_text(node) if node else None


def parse_vacancy_page(html: str) -> str:
    vacancy = BeautifulSoup(html, "lxml").select_one("article.vacancy")
    description = vacancy.select_one("#vacancy-description") if vacancy else None
    if vacancy is None or description is None:
        raise SourceError("geekjob.ru: vacancy page has no description block (layout changed?)")
    tags = " • ".join(t.get_text(" ", strip=True) for t in vacancy.select("header .tags") if t.get_text(strip=True))
    grade = _node_text(vacancy.select_one("header .category"))
    salary = _node_text(vacancy.select_one("header .salary"))
    return card_text(
        f"Уровень: {grade}" if grade else None,
        f"Теги: {tags}" if tags else None,
        _node_text(vacancy.select_one("header .jobformat")),
        f"Зарплата: {salary}" if salary else None,
        html_to_text(description),
    )


@register
class GeekjobCollector(Collector):
    """geekjob.ru open JSON search (`/json/find/vacancy`, 100 per page); details are parsed from the vacancy page."""

    type_name = "geekjob"

    def now(self) -> datetime:
        return datetime.now(UTC)

    def params(self) -> dict[str, Any]:
        return {**DEFAULT_PARAMS, **self.source.params}

    def query(self, page: int) -> dict[str, object]:
        params = self.params()
        query: dict[str, object] = {"page": page}
        if params["remote"]:
            query["rm"] = 1
        if params["relocate"]:
            query["rl"] = 1
        if params["tags"]:
            query["t"] = ",".join(str(tag) for tag in params["tags"])
        return query

    def collect(self, ctx: CollectContext) -> CollectResult:
        now = self.now()
        crawler = ListingCrawler(ctx)
        crawler.crawl(lambda page: self._fetch_page(page, now))
        return crawler.result()

    def _fetch_page(self, page: int, now: datetime) -> tuple[list[RawItem], bool]:
        data = self.http.get_json(self.source.url or API_URL, **self.query(page))
        return parse_vacancies(data, now, self.source.id), int(data.get("nextpage") or 0) > page

    def fetch_detail(self, item: RawItem) -> str | None:
        if not item.detail_url:
            return None
        return parse_vacancy_page(self.http.get_text(item.detail_url))

    def icon_url(self) -> str | None:
        return f"{GEEKJOB_BASE}/favicon/favicon-32x32.png"
