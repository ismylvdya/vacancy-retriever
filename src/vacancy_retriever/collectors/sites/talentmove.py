from __future__ import annotations

import re
from email.utils import parsedate_to_datetime

import httpx
from bs4 import BeautifulSoup, Tag

from ..base import CollectContext, Collector, CollectResult, CollectStatus, RawItem
from ..registry import register
from ._common import html_to_text, stop_date

SITE_URL = "https://talent-move.ru"
FEED_URL = f"{SITE_URL}/jobs/feed/"
DEFAULT_FILTERS = {"seniority": "junior", "job_category": "data,analytics,ai"}
MAX_TEXT_CHARS = 8000
_POST_ID = re.compile(r"-(\d+)/?$")
_DETAIL_SKIP = ("job-header", "job-quality", "job-bun", "job-footer")


def feed_page_url(filters: dict[str, str], page: int) -> str:
    query = dict(filters)
    if page > 1:
        query["paged"] = str(page)
    return str(httpx.URL(FEED_URL, params=query))


def parse_feed(xml: str) -> list[RawItem]:
    """Items of one RSS page of the `job` post type, newest first, with full descriptions."""
    soup = BeautifulSoup(xml, "xml")
    return [item for item in (_feed_item(node) for node in soup.find_all("item")) if item]


def _feed_item(node: Tag) -> RawItem | None:
    link = _child_text(node, "link")
    match = _POST_ID.search(link)
    if not match:
        return None
    title = _child_text(node, "title")
    summary = _child_text(node, "description")
    content = node.find("encoded")
    body = html_to_text(BeautifulSoup(content.get_text(), "lxml")) if content else ""
    text = "\n\n".join(part for part in (title, summary, body) if part)
    pub_date = _child_text(node, "pubDate")
    return RawItem(
        source_id="",
        external_id=match.group(1),
        url=link,
        title=title,
        text=text[:MAX_TEXT_CHARS],
        published_at=parsedate_to_datetime(pub_date) if pub_date else None,
        detail_url=link,
        meta={"company": None, "location": None},
    )


def _child_text(node: Tag, name: str) -> str:
    child = node.find(name)
    return child.get_text(strip=True) if child else ""


def parse_detail(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    article = soup.select_one("article.type-job") or soup.select_one("article")
    if article is None:
        return ""
    header = article.select_one("header.job-header")
    lines: list[str] = []
    if header:
        title = header.select_one("h1")
        if title:
            lines.append(title.get_text(" ", strip=True))
        company = header.select_one(".job-features__item--company")
        if company:
            lines.append(f"Компания: {company.get_text(' ', strip=True)}")
        features = [li.get_text(" ", strip=True) for li in header.select(".job-features__item")
                    if "job-features__item--company" not in li.get("class", [])]
        if features:
            lines.append(" · ".join(features))
        tags = [li.get_text(" ", strip=True) for li in header.select(".job-tags__tag")]
        if tags:
            lines.append(f"Теги: {', '.join(tags)}")
    for child in article.find_all(recursive=False):
        if any(cls in child.get("class", []) for cls in _DETAIL_SKIP):
            child.decompose()
    body = html_to_text(article)
    return "\n".join(lines + ["", body]).strip()[:MAX_TEXT_CHARS]


@register
class TalentMoveCollector(Collector):
    """talent-move.ru: WordPress `job` posts via the filtered RSS feed (dates + full text), detail page for company."""

    type_name = "talentmove"

    def filters(self) -> dict[str, str]:
        return {k: str(v) for k, v in (self.source.params.get("filters") or DEFAULT_FILTERS).items()}

    def collect(self, ctx: CollectContext) -> CollectResult:
        stop_before = stop_date(ctx)
        found: dict[str, RawItem] = {}
        for page in range(1, ctx.max_pages + 1):
            ctx.check_cancel()
            ctx.on_page(page)
            items = self._page(page)
            if not items or all(ctx.is_known(item.external_id) for item in items):
                return self._result(found, ctx, "ok")
            for item in items:
                if item.published_at and item.published_at < stop_before:
                    return self._result(found, ctx, "ok")
                item.source_id = self.source.id
                found.setdefault(item.external_id, item)
                if ctx.limit and len(found) >= ctx.limit:
                    return self._result(found, ctx, "ok")
        return self._result(found, ctx, "truncated")

    def _page(self, page: int) -> list[RawItem]:
        try:
            return parse_feed(self.http.get_text(feed_page_url(self.filters(), page)))
        except httpx.HTTPStatusError as exc:
            if page > 1 and exc.response.status_code == 404:
                return []
            raise

    @staticmethod
    def _result(found: dict[str, RawItem], ctx: CollectContext, status: CollectStatus) -> CollectResult:
        items = list(found.values())
        return CollectResult(items=items[: ctx.limit] if ctx.limit else items, status=status)

    def fetch_detail(self, item: RawItem) -> str | None:
        return parse_detail(self.http.get_text(item.detail_url or item.url)) or None

    def icon_url(self) -> str | None:
        return f"{SITE_URL}/wp-content/uploads/2024/11/cropped-tm-logo-32x32-optimized.png"
