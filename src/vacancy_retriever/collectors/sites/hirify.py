from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..base import CollectContext, Collector, CollectResult, CollectStatus, RawItem, SourceError
from ..registry import register

SITE_URL = "https://hirify.me"
API_URL = "https://api.hirify.me/api/vacancies"
DEFAULT_LISTINGS = (
    f"{SITE_URL}/?countries=serbia&grade=junior,trainee",
    f"{SITE_URL}/?specializations=data_science,machine_learning,data_analytics,ai_engineering,product_analyst"
    "&grade=junior,trainee&work_format=remote&vacancy_language=ru",
)
MAX_TEXT_CHARS = 8000
_WS = re.compile(r"[ \t\r\f\v\xa0]+")
_BLOCKS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "blockquote", "pre")
_BULLET = "•"
_HIDDEN_COMPANY = "%hirify_global%"
_WORK_FORMATS = {"remote": "удалённо", "hybrid": "гибрид", "onsite": "офис"}


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


def api_listing_url(listing: str) -> str:
    """hirify.me/?<filters> (what the user sees in the browser) -> the API URL serving the same feed."""
    query = urlsplit(listing).query
    return f"{API_URL}?{query}" if query else API_URL


def page_url(api_url: str, page: int) -> str:
    """Built by hand: httpx drops the URL's own query string when `params` are passed."""
    return f"{api_url}{'&' if '?' in api_url else '?'}page={page}"


def detail_api_url(vacancy_id: int | str) -> str:
    return f"{API_URL}/{vacancy_id}"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace(" ", "T"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def bumped_at(item: RawItem) -> datetime | None:
    """The feed is ordered by `updated_at` (vacancies get bumped), not by publication date."""
    return parse_date(item.meta.get("updated_at"))


def parse_vacancies(payload: dict[str, Any]) -> tuple[list[RawItem], bool]:
    items = [vacancy_item(vacancy) for vacancy in payload.get("data") or []]
    return items, bool(payload.get("next_page_url"))


def vacancy_item(vacancy: dict[str, Any]) -> RawItem:
    title = (vacancy.get("title") or "").strip()
    company = _company(vacancy)
    regions = ", ".join(r["name"] for r in vacancy.get("regions") or [] if r.get("name"))
    formats = ", ".join(_WORK_FORMATS.get(f, f) for f in vacancy.get("work_format") or [])
    grades = ", ".join(g["name"] for g in vacancy.get("grades") or [] if g.get("name"))
    stack = ", ".join(vacancy.get("main_stack") or [])
    published = max(
        (d for d in (parse_date(vacancy.get("created_at")), parse_date(vacancy.get("reopened_at"))) if d),
        default=None,
    )
    header = [
        title,
        f"Компания: {company}" if company else "",
        f"Регионы: {regions}" if regions else "",
        f"Формат: {formats}" if formats else "",
        f"Remote: {vacancy['remote_type']}" if vacancy.get("remote_type") else "",
        f"Грейд: {grades}" if grades else "",
        f"Зарплата: {_salary(vacancy.get('salary'))}" if vacancy.get("salary") else "",
        f"Язык вакансии: {vacancy['vacancy_language']}" if vacancy.get("vacancy_language") else "",
        f"Стек: {stack}" if stack else "",
    ]
    text = "\n".join(line for line in header if line)
    if vacancy.get("tldr"):
        text = f"{text}\n\n{vacancy['tldr'].strip().replace(_HIDDEN_COMPANY, 'The company')}"
    return RawItem(
        source_id="",
        external_id=str(vacancy["id"]),
        url=f"{SITE_URL}/jobs/{vacancy.get('slug') or vacancy['id']}",
        title=title,
        text=text[:MAX_TEXT_CHARS],
        published_at=published,
        detail_url=detail_api_url(vacancy["id"]),
        meta={
            "company": company,
            "location": regions or None,
            "grade": grades or None,
            "updated_at": vacancy.get("updated_at"),
        },
    )


def _company(vacancy: dict[str, Any]) -> str | None:
    name = (vacancy.get("company_title") or (vacancy.get("company") or {}).get("name") or "").strip()
    return None if not name or name == _HIDDEN_COMPANY else name


def _salary(salary: dict[str, Any]) -> str:
    bounds = " - ".join(str(salary[k]) for k in ("min", "max") if salary.get(k))
    period = f" / {salary['salary_period']}" if salary.get("salary_period") else ""
    return f"{bounds} {salary.get('currency') or ''}{period}".strip()


def parse_detail(payload: dict[str, Any]) -> str:
    item = vacancy_item(payload)
    header = item.text.split("\n\n", 1)[0]
    body = html_to_text(payload.get("text") or "") or (payload.get("tldr") or "").strip()
    text = f"{header}\n\n{body}".strip().replace(_HIDDEN_COMPANY, "The company")
    return text[:MAX_TEXT_CHARS]


@register
class HirifyCollector(Collector):
    """hirify.me feeds via api.hirify.me (the JSON API behind the site; listing URLs keep site filters)."""

    type_name = "hirify"

    def listings(self) -> list[str]:
        return list(self.source.params.get("listings") or DEFAULT_LISTINGS)

    def collect(self, ctx: CollectContext) -> CollectResult:
        found: dict[str, RawItem] = {}
        truncated = False
        pages_done = 0
        for listing in self.listings():
            status, pages = self._collect_listing(ctx, api_listing_url(listing), found, pages_done)
            pages_done += pages
            truncated |= status == "truncated"
            if ctx.limit and len(found) >= ctx.limit:
                break
        items = list(found.values())
        return CollectResult(items=items[: ctx.limit] if ctx.limit else items, status="truncated" if truncated else "ok")

    def _collect_listing(
        self, ctx: CollectContext, api_url: str, found: dict[str, RawItem], pages_before: int
    ) -> tuple[CollectStatus, int]:
        stop_before = _stop_before(ctx)
        for page in range(1, ctx.max_pages + 1):
            ctx.check_cancel()
            ctx.on_page(pages_before + page)
            payload = self.http.get_json(page_url(api_url, page))
            if not isinstance(payload, dict):
                raise SourceError(f"hirify: unexpected listing payload from {api_url}")
            items, has_more = parse_vacancies(payload)
            if not items or all(ctx.is_known(item.external_id) for item in items):
                return "ok", page
            for item in items:
                bumped = bumped_at(item)
                if bumped and bumped < stop_before:
                    return "ok", page
                if item.published_at and item.published_at < ctx.since:
                    continue
                item.source_id = self.source.id
                found.setdefault(item.external_id, item)
                if ctx.limit and len(found) >= ctx.limit:
                    return "ok", page
            if not has_more:
                return "ok", page
        return "truncated", ctx.max_pages

    def fetch_detail(self, item: RawItem) -> str | None:
        payload = self.http.get_json(item.detail_url or detail_api_url(item.external_id))
        if not isinstance(payload, dict):
            raise SourceError(f"hirify: unexpected detail payload for {item.external_id}")
        return parse_detail(payload) or None

    def icon_url(self) -> str | None:
        return f"{SITE_URL}/favicon-96x96.png"


def _stop_before(ctx: CollectContext) -> datetime:
    if ctx.cursor.last_published_at is None:
        return ctx.since
    return max(ctx.since, ctx.cursor.last_published_at - timedelta(days=1))
