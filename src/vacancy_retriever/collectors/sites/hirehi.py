"""hirehi.ru: listings are server-rendered HTML (`a.job-card`, newest first, `?page=N`); the vacancy page carries a
schema.org JobPosting with the full description (decision of the user 20.11.2026: Russia, intern / junior)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError
from ..registry import register
from ._common import ListingCrawler, card_text

SITE_URL = "https://hirehi.ru"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def page_url(listing: str, page: int) -> str:
    """Built by hand: the filters repeat keys (`format=…&format=…`), which must be kept as they are."""
    if page <= 1:
        return listing
    parts = urlsplit(listing)
    query = f"{parts.query}&page={page}" if parts.query else f"page={page}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _text(node: Tag | None) -> str:
    return node.get_text(" ", strip=True) if node else ""


def card_item(card: Tag, source_id: str = "") -> RawItem | None:
    vacancy_id, href = card.get("data-id"), card.get("href")
    if not vacancy_id or not href:
        return None
    url = f"{SITE_URL}{href}" if str(href).startswith("/") else str(href)
    title = _text(card.select_one(".job-title"))
    company = _text(card.select_one(".job-company-name-text"))
    salary = _text(card.select_one(".job-salary-value"))
    tags = [_text(t) for t in card.select(".job-tag") if _text(t)]
    formats = [_text(t) for t in card.select(".job-format") if _text(t)]
    date = card.select_one(".job-published-date")
    return RawItem(
        source_id=source_id,
        external_id=str(vacancy_id),
        url=url,
        title=title,
        text=card_text(title, f"Компания: {company}" if company else None, f"Зарплата: {salary}" if salary else None,
                       f"Теги: {', '.join(tags)}" if tags else None),
        published_at=parse_time(date.get("data-date") if date else None),  # type: ignore[arg-type]
        detail_url=url,
        meta={"company": company or None, "location": ", ".join(formats) or None,
              "category": card.get("data-category")},
    )


def parse_listing(html: str, source_id: str = "") -> tuple[list[RawItem], bool]:
    soup = BeautifulSoup(html, "lxml")
    cards = [item for card in soup.select("a.job-card") if (item := card_item(card, source_id))]
    pagination = soup.select_one("[data-total-pages]")
    if pagination is None and not cards:
        raise SourceError("hirehi.ru: no vacancy cards and no pagination (layout changed?)")
    has_next = False
    if pagination is not None:
        try:
            has_next = int(pagination.get("data-page") or 1) < int(pagination.get("data-total-pages") or 1)
        except ValueError:
            has_next = False
    return cards, has_next


def parse_vacancy_page(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            organization = (data.get("hiringOrganization") or {}).get("name")
            address = ((data.get("jobLocation") or {}).get("address") or {})
            place = ", ".join(str(address[k]) for k in ("addressLocality", "addressCountry")
                              if isinstance(address, dict) and address.get(k))
            return card_text(
                str(data.get("title") or ""),
                f"Компания: {organization}" if organization else None,
                f"Место: {place}" if place else None,
                f"Удалённо: {data['jobLocationType']}" if data.get("jobLocationType") else None,
                str(data.get("description") or ""),
            )
    raise SourceError("hirehi.ru: vacancy page has no JobPosting (layout changed?)")


@register
class HirehiCollector(Collector):
    """`params.listings`: hirehi.ru filter links copied from the browser (a category path like /vacancies/ml-ai keeps
    the site's own filters)."""

    type_name = "hirehi"

    def listings(self) -> list[str]:
        listings = list(self.source.params.get("listings") or [])
        return listings or ([self.source.url] if self.source.url else [SITE_URL])

    def collect(self, ctx: CollectContext) -> CollectResult:
        found: dict[str, RawItem] = {}
        truncated = False
        for listing in self.listings():
            crawler = ListingCrawler(ctx)
            crawler.crawl(lambda page, listing=listing: parse_listing(self.http.get_text(page_url(listing, page)),
                                                                      self.source.id))
            truncated = truncated or crawler.truncated
            for item in crawler.items:
                found.setdefault(item.external_id, item)
            if ctx.limit is not None and len(found) >= ctx.limit:
                break
        items = list(found.values())[: ctx.limit] if ctx.limit else list(found.values())
        return CollectResult(items=items, status="truncated" if truncated else "ok", new_cursor=None)

    def fetch_detail(self, item: RawItem) -> str | None:
        return parse_vacancy_page(self.http.get_text(item.detail_url)) if item.detail_url else None

    def icon_url(self) -> str | None:
        return f"{SITE_URL}/favicon.ico"
