from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from ..base import CollectContext, Collector, CollectResult, RawItem
from ..registry import register
from ._common import ListingCrawler, card_text, html_to_text

GETMATCH_BASE = "https://getmatch.ru"
API_URL = f"{GETMATCH_BASE}/api/offers"
DEFAULT_PARAMS: dict[str, Any] = {
    "period": "30d",
    "seniority": ["junior"],
    "specializations": ["data_engineering", "data_science", "python", "data_analyst_bi"],
    "locations": ["remote", "relocate"],
    "page_size": 50,
}
_FORMATS = {"remote": "удалённо", "relocation_company": "релокация", "office": "офис", "hybrid": "гибрид"}
_ZERO_WIDTH_JOINER = "‍"


def fragment_text(html: str | None) -> str:
    return html_to_text(BeautifulSoup(html, "lxml")) if html else ""


def parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    published = datetime.fromisoformat(value)
    return published.replace(tzinfo=UTC) if published.tzinfo is None else published.astimezone(UTC)


def offer_locations(offer: dict[str, Any]) -> list[str]:
    places: dict[str, list[str]] = {}
    for loc in offer.get("location_items") or []:
        if loc.get("exclude"):
            continue
        formats = places.setdefault(loc.get("label") or "", [])
        fmt = _FORMATS.get(loc.get("format"), loc.get("format") or "")
        if fmt and fmt not in formats:
            formats.append(fmt)
    return [f"{label} ({', '.join(fmts)})" if fmts else label for label, fmts in places.items()]


def offer_salary(offer: dict[str, Any]) -> str | None:
    if offer.get("salary_hidden") or not offer.get("salary_description"):
        return None
    return offer["salary_description"].replace(_ZERO_WIDTH_JOINER, "")


def offer_text(offer: dict[str, Any]) -> str:
    company = (offer.get("company") or {}).get("name")
    locations = offer_locations(offer)
    skills = [s["name"] for s in offer.get("skills_objects") or [] if s.get("name")]
    salary = offer_salary(offer)
    return card_text(
        offer.get("position"),
        f"Компания: {company}" if company else None,
        f"Локация: {'; '.join(locations)}" if locations else None,
        f"Зарплата: {salary}" if salary else None,
        f"Английский: {offer['english_level']}" if offer.get("english_level") else None,
        f"Навыки: {', '.join(skills)}" if skills else None,
        fragment_text(offer.get("offer_description")),
    )


def offer_item(offer: dict[str, Any], source_id: str = "") -> RawItem:
    offer_id = str(offer["id"])
    return RawItem(
        source_id=source_id,
        external_id=offer_id,
        url=GETMATCH_BASE + (offer.get("url") or f"/vacancies/{offer_id}"),
        title=offer.get("position") or "",
        text=offer_text(offer),
        published_at=parse_published(offer.get("published_at")),
        detail_url=f"{API_URL}/{offer_id}",
        meta={
            "company": (offer.get("company") or {}).get("name"),
            "location": "; ".join(offer_locations(offer)) or None,
            "language": offer.get("language"),
        },
    )


def parse_offers(data: dict[str, Any], source_id: str = "") -> list[RawItem]:
    return [offer_item(offer, source_id) for offer in data.get("offers") or [] if offer.get("id")]


def offer_detail_text(offer: dict[str, Any]) -> str:
    seniorities = ", ".join(offer.get("seniorities") or [])
    years = offer.get("required_years_of_experience")
    stack = fragment_text(offer.get("stack_description"))
    return card_text(
        f"Грейд: {seniorities}" if seniorities else None,
        f"Опыт (лет): {years}" if years else None,
        f"Команда: {offer['team_size']}" if offer.get("team_size") else None,
        fragment_text(offer.get("short_description")),
        fragment_text(offer.get("description") or offer.get("description_html")),
        f"Стек:\n{stack}" if stack else None,
    )


@register
class GetmatchCollector(Collector):
    """getmatch.ru open JSON API: /api/offers listing (offset paging) and /api/offers/<id> details."""

    type_name = "getmatch"

    def params(self) -> dict[str, Any]:
        return {**DEFAULT_PARAMS, **self.source.params}

    def query(self, offset: int) -> dict[str, object]:
        params = self.params()
        return {
            "pa": params["period"],
            "se": params["seniority"],
            "sp": params["specializations"],
            "l": params["locations"],
            "offset": offset,
            "limit": params["page_size"],
        }

    def collect(self, ctx: CollectContext) -> CollectResult:
        crawler = ListingCrawler(ctx)
        crawler.crawl(self._fetch_page)
        return crawler.result()

    def _fetch_page(self, page: int) -> tuple[list[RawItem], bool]:
        offset = (page - 1) * int(self.params()["page_size"])
        data = self.http.get_json(self.source.url or API_URL, **self.query(offset))
        offers = data.get("offers") or []
        total = (data.get("meta") or {}).get("total", 0)
        return parse_offers(data, self.source.id), bool(offers) and offset + len(offers) < total

    def fetch_detail(self, item: RawItem) -> str | None:
        if not item.detail_url:
            return None
        return offer_detail_text(self.http.get_json(item.detail_url))

    def icon_url(self) -> str | None:
        return f"{GETMATCH_BASE}/uploads/favicons/favicon.ico"
