from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..base import CollectContext, Collector, CollectResult, CollectStatus, RawItem, SourceError
from ..registry import register

BASE_URL = "https://youngjunior.ru"
API_URL = f"{BASE_URL}/api/vacancies/"
DEFAULT_SPECIALITIES = ("ml_engineer", "data_scientist", "analyst_data")
DEFAULT_PAGE_SIZE = 50
MAX_TEXT_CHARS = 8000
_WORK_FORMATS = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис", "onsite": "офис"}
_WS = re.compile(r"[ \t\r\f\v\xa0]+")
_BLOCKS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "blockquote", "pre")
_BULLET = "•"


def html_to_text(html: str) -> str:
    node = BeautifulSoup(html, "lxml")
    for br in node.find_all("br"):
        br.replace_with("\n")
    for block in node.find_all(_BLOCKS):
        block.insert_before("\n")
        block.insert_after("\n")
    for li in node.find_all("li"):
        li.insert(0, f"{_BULLET} ")
    lines: list[str] = []
    for raw in node.get_text().splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line:
            continue
        if lines and lines[-1] == _BULLET:
            lines[-1] = f"{_BULLET} {line}"
        else:
            lines.append(line)
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def vacancy_url(vacancy_id: int | str) -> str:
    return f"{BASE_URL}/data/jobs/{vacancy_id}"


def parse_vacancies(payload: dict[str, Any]) -> tuple[list[RawItem], bool]:
    """Items of one API page (newest first) and whether more pages exist."""
    items = [vacancy_item(vacancy) for vacancy in payload.get("items") or []]
    return items, bool(payload.get("has_more"))


def vacancy_item(vacancy: dict[str, Any]) -> RawItem:
    title = (vacancy.get("title") or "").strip()
    company = ((vacancy.get("company") or {}).get("name") or "").strip() or None
    location = ", ".join(part for part in (vacancy.get("city"), vacancy.get("country")) if part) or None
    work_format = _WORK_FORMATS.get(vacancy.get("work_format") or "", vacancy.get("work_format"))
    tags = ", ".join(tag["name"] for tag in vacancy.get("tags") or [] if tag.get("name"))
    header = [
        title,
        f"Компания: {company}" if company else "",
        f"Локация: {location}" if location else "",
        f"Формат: {work_format}" if work_format else "",
        "Стажировка" if vacancy.get("internship") else "",
        f"Грейд: {vacancy['grade']}" if vacancy.get("grade") else "",
        f"Зарплата: {vacancy['salary']}" if vacancy.get("salary") else "",
        f"Теги: {tags}" if tags else "",
    ]
    description = html_to_text(vacancy.get("description") or vacancy.get("summary") or "")
    text = "\n".join(line for line in header if line)
    if description:
        text = f"{text}\n\n{description}"
    return RawItem(
        source_id="",
        external_id=str(vacancy["id"]),
        url=vacancy_url(vacancy["id"]),
        title=title,
        text=text[:MAX_TEXT_CHARS],
        published_at=_parse_date(vacancy.get("date_publication")),
        detail_url=None,
        meta={
            "company": company,
            "location": location,
            "grade": vacancy.get("grade"),
            "remote": bool(vacancy.get("remote")),
            "original_url": vacancy.get("source_url") or vacancy.get("url"),
        },
    )


def _parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@register
class YoungJuniorCollector(Collector):
    """youngjunior.ru via the JSON API its own /<section>/jobs pages use (newest first, full descriptions)."""

    type_name = "youngjunior"

    def query(self, skip: int, limit: int) -> dict[str, object]:
        params = self.source.params
        query: dict[str, object] = {
            "speciality": ",".join(params.get("specialities") or DEFAULT_SPECIALITIES),
            "skip": skip,
            "limit": limit,
            "sort_by": "date_publication",
            "order": "desc",
        }
        if params.get("remote", True):
            query["remote"] = "true"
        return query

    def collect(self, ctx: CollectContext) -> CollectResult:
        page_size = int(self.source.params.get("page_size") or DEFAULT_PAGE_SIZE)
        stop_before = _stop_before(ctx)
        found: dict[str, RawItem] = {}
        for page in range(1, ctx.max_pages + 1):
            ctx.check_cancel()
            ctx.on_page(page)
            payload = self.http.get_json(API_URL, **self.query((page - 1) * page_size, page_size))
            if not isinstance(payload, dict):
                raise SourceError("youngjunior: unexpected API payload")
            items, has_more = parse_vacancies(payload)
            if not items or all(ctx.is_known(item.external_id) for item in items):
                return self._result(found, ctx, "ok")
            for item in items:
                if item.published_at and item.published_at < stop_before:
                    return self._result(found, ctx, "ok")
                item.source_id = self.source.id
                found.setdefault(item.external_id, item)
                if ctx.limit and len(found) >= ctx.limit:
                    return self._result(found, ctx, "ok")
            if not has_more:
                return self._result(found, ctx, "ok")
        return self._result(found, ctx, "truncated")

    @staticmethod
    def _result(found: dict[str, RawItem], ctx: CollectContext, status: CollectStatus) -> CollectResult:
        items = list(found.values())
        return CollectResult(items=items[: ctx.limit] if ctx.limit else items, status=status)

    def icon_url(self) -> str | None:
        return f"{BASE_URL}/favicon.ico"


def _stop_before(ctx: CollectContext) -> datetime:
    """Listing is sorted by date: anything older than `since` or the cursor (minus a day of slack) ends the walk."""
    if ctx.cursor.last_published_at is None:
        return ctx.since
    return max(ctx.since, ctx.cursor.last_published_at - timedelta(days=1))
