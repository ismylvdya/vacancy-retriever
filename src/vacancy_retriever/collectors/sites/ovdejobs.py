from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from bs4 import BeautifulSoup, Tag

from ..base import CollectContext, Collector, CollectResult, RawItem
from ..registry import register
from ._common import ListingCrawler, card_text, favicon_from_html, html_to_text, join_lines

BASE = "https://ovdejobs.com"
LISTING_URL = f"{BASE}/vacancies/"
DATES_URL = f"{BASE}/wp-json/wp/v2/jobs"
DEFAULT_CITIES = ("belgrade", "remote")

_PAGER = re.compile(r'"pager":(\{[^{}]*\})')
_DETAIL_NOISE = (
    "details, .elementor-widget-n-accordion, .elementor-widget-button, .elementor-widget-form, "
    ".elementor-widget-image, .elementor-widget-icon, .elementor-widget-nav-menu"
)
_DETAIL_BOILERPLATE = {"Easy Apply", "Apply now", "Back to all jobs"}


@dataclass(frozen=True)
class OvdeCard:
    job_id: str
    url: str
    title: str
    company: str
    city: str
    salary: str


def _text(node: Tag | None) -> str:
    return node.get_text(" ", strip=True) if node else ""


def parse_listing(html: str) -> tuple[list[OvdeCard], int]:
    """Cards of one FacetWP listing page and the total number of pages."""
    soup = BeautifulSoup(html, "lxml")
    cards: list[OvdeCard] = []
    for link in soup.select(".facetwp-template a.fwp-job-card-link[href]"):
        card = link.select_one(".fwp-job-card[data-job-id]")
        if card is None:
            continue
        cards.append(
            OvdeCard(
                job_id=card["data-job-id"],
                url=link["href"],
                title=_text(card.select_one(".fwp-job-title")),
                company=_text(card.select_one(".fwp-company-name")),
                city=_text(card.select_one(".fwp-job-city")),
                salary=_text(card.select_one(".fwp-job-salary")),
            )
        )
    return cards, _total_pages(html)


def _total_pages(html: str) -> int:
    match = _PAGER.search(html)
    if not match:
        return 1
    return int(json.loads(match.group(1)).get("total_pages") or 0)


def parse_dates(payload: object) -> dict[str, datetime]:
    """`date_gmt` of WP REST /jobs records keyed by post id."""
    dates: dict[str, datetime] = {}
    for record in payload if isinstance(payload, list) else []:
        raw = record.get("date_gmt")
        if raw:
            dates[str(record["id"])] = datetime.fromisoformat(raw).replace(tzinfo=UTC)
    return dates


def to_item(card: OvdeCard, source_id: str, published_at: datetime | None) -> RawItem:
    return RawItem(
        source_id=source_id,
        external_id=card.job_id,
        url=card.url,
        title=card.title,
        text=card_text(
            card.title,
            card.company,
            f"Location: {card.city}" if card.city else None,
            f"Salary: {card.salary}" if card.salary else None,
        ),
        published_at=published_at,
        detail_url=card.url,
        meta={"company": card.company or None, "location": card.city or None},
    )


def parse_detail(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(".elementor-location-single") or soup.body
    if root is None:
        return ""
    for noise in root.select(_DETAIL_NOISE):
        noise.decompose()
    heading = root.find("h1")
    title = _text(heading)
    lines = html_to_text(root).splitlines()
    if title in lines:
        lines = lines[lines.index(title):]
    return join_lines(line for line in lines if line not in _DETAIL_BOILERPLATE)


@register
class OvdeJobsCollector(Collector):
    """ovdejobs.com: FacetWP listing (company, city, salary) + WP REST for publication dates."""

    type_name = "ovdejobs"

    @property
    def cities(self) -> list[str]:
        return list(self.source.params.get("cities") or DEFAULT_CITIES)

    @property
    def categories(self) -> list[str | None]:
        return list(self.source.params.get("categories") or [None])

    def collect(self, ctx: CollectContext) -> CollectResult:
        crawler = ListingCrawler(ctx)
        for city in self.cities:
            for category in self.categories:
                crawler.crawl(lambda page, c=city, cat=category: self._fetch_page(c, cat, page))
                if crawler.limit_reached:
                    return crawler.result()
        return crawler.result()

    def _fetch_page(self, city: str, category: str | None, page: int) -> tuple[list[RawItem], bool]:
        params: dict[str, object] = {"_job_city": city}
        if category:
            params["_job_category"] = category
        if page > 1:
            params["_paged"] = page
        cards, total_pages = parse_listing(self.http.get_text(LISTING_URL, **params))
        if not cards:
            return [], False
        dates = parse_dates(
            self.http.get_json(
                DATES_URL, include=",".join(c.job_id for c in cards), per_page=len(cards), _fields="id,date_gmt"
            )
        )
        items = [to_item(card, self.source.id, dates.get(card.job_id)) for card in cards]
        return items, page < total_pages

    def fetch_detail(self, item: RawItem) -> str | None:
        return parse_detail(self.http.get_text(item.detail_url or item.url)) or None

    def icon_url(self) -> str | None:
        return favicon_from_html(self.http.get_text(BASE), BASE)
