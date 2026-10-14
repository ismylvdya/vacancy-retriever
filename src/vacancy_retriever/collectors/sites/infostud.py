from __future__ import annotations

import json
import re
from datetime import UTC, datetime, time
from typing import Any

from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError
from ..registry import register
from ._common import ListingCrawler, card_text, favicon_from_html, fragment_text, join_lines, stop_date

BASE = "https://poslovi.infostud.com"
LISTING_PATH = "/oglasi-za-posao/{city}"
DEFAULT_CITY = "beograd"

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(html: str) -> dict[str, Any]:
    match = _NEXT_DATA.search(html)
    if not match:
        raise SourceError("infostud: no __NEXT_DATA__ on the page (layout changed or blocked)", alert="layout")
    return json.loads(match.group(1))


def parse_day(raw: str | None, now: datetime | None = None) -> datetime | None:
    """`06.10.2026` / `06.10.2026.` / `2026-10-07` → end of that day (UTC), never in the future.

    The site only gives a date; end of day keeps a vacancy posted earlier today inside the run window."""
    if not raw:
        return None
    raw = raw.strip().rstrip(".")
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            day = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        moment = datetime.combine(day, time(23, 59), tzinfo=UTC)
        return min(moment, now or datetime.now(UTC))
    return None


def parse_listing(html: str, source_id: str, now: datetime | None = None) -> tuple[list[RawItem], int]:
    """Jobs of one listing page and the total number of matching jobs."""
    results = next_data(html)["props"]["pageProps"].get("initialSearchResults") or {}
    jobs = (results.get("jobs") or {}).get("primary") or []
    items = [to_item(job, source_id, now) for job in jobs if job.get("id") and job.get("url")]
    return items, int(results.get("totalPrimaryItems") or 0)


def to_item(job: dict[str, Any], source_id: str, now: datetime | None = None) -> RawItem:
    position = (job.get("standardizedPosition") or {}).get("name")
    category = (job.get("primaryCategory") or {}).get("name")
    summary = (job.get("jobSummary") or {}).get("summary")
    work_place = "remote" if job.get("workFromHome") else "hybrid" if job.get("hybridWork") else None
    salary = job.get("salary")
    return RawItem(
        source_id=source_id,
        external_id=str(job["id"]),
        url=job["url"],
        title=job.get("title") or "",
        text=card_text(
            job.get("title"),
            job.get("companyName"),
            f"Location: {job['location']}" if job.get("location") else None,
            f"Work place: {work_place}" if work_place else None,
            f"Category: {category}" if category else None,
            f"Position: {position}" if position else None,
            f"Salary: {salary}" if isinstance(salary, str) and salary else None,
            summary,
        ),
        published_at=parse_day(job.get("onlineViewDate"), now),
        detail_url=job["url"],
        meta={"company": job.get("companyName") or None, "location": job.get("location") or None},
    )


def parse_detail(html: str) -> str:
    job = next_data(html)["props"]["pageProps"].get("job") or {}
    employment = (job.get("employmentType") or {}).get("nameSr")
    hours = (job.get("workingHours") or {}).get("nameSr")
    lines = [
        job.get("title"),
        job.get("companyDisplayName") or job.get("companyName"),
        f"Location: {job['location']}" if job.get("location") else None,
        f"Employment: {employment}" if employment else None,
        f"Working hours: {hours}" if hours else None,
    ]
    head = [line for line in lines if line]
    body = fragment_text(job.get("textAd"))
    return join_lines([*head, *body.splitlines()])


@register
class InfostudCollector(Collector):
    """poslovi.infostud.com (also covers startuj.infostud.com via `youth: true` and helloworld.rs —
    the same job ids). Listing data comes from Next.js `__NEXT_DATA__`; the site filters by date."""

    type_name = "infostud"

    @property
    def page_size(self) -> int:
        return int(self.source.params.get("page_size") or 30)

    def collect(self, ctx: CollectContext) -> CollectResult:
        crawler = ListingCrawler(ctx)
        since = stop_date(ctx)
        crawler.crawl(lambda page: self._fetch_page(page, since))
        return crawler.result()

    def query(self, page: int, since: datetime) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for index, category in enumerate(self.source.params.get("categories") or []):
            params[f"category[{index}]"] = category
        if self.source.params.get("youth"):
            params["youthPosition"] = 1
        if self.source.params.get("dist"):
            params["dist"] = self.source.params["dist"]
        params["onlineAfterDate"] = since.date().isoformat()
        if page > 1:
            params["page"] = page
        return params

    def _fetch_page(self, page: int, since: datetime) -> tuple[list[RawItem], bool]:
        city = self.source.params.get("city") or DEFAULT_CITY
        html = self.http.get_text(BASE + LISTING_PATH.format(city=city), **self.query(page, since))
        items, total = parse_listing(html, self.source.id)
        return items, page * self.page_size < total

    def fetch_detail(self, item: RawItem) -> str | None:
        return parse_detail(self.http.get_text(item.detail_url or item.url)) or None

    def icon_url(self) -> str | None:
        return favicon_from_html(self.http.get_text(BASE), BASE)
