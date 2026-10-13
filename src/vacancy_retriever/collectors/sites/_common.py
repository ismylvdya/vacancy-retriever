from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from ..base import CollectContext, CollectResult, RawItem

DETAIL_TEXT_LIMIT = 8000

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "details", "div", "dl", "dt", "figcaption", "figure",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "summary", "table", "td", "th", "tr", "ul",
}
_NOISE_TAGS = ("script", "style", "noscript", "svg", "form", "iframe", "button", "template")
_SPACES = re.compile(r"[ \t ​]+")

PageFetcher = Callable[[int], tuple[list[RawItem], bool]]


def html_to_text(node: Tag, limit: int = DETAIL_TEXT_LIMIT) -> str:
    """Readable plain text: one line per block element / <br>, whitespace collapsed."""
    for noise in node.find_all(_NOISE_TAGS):
        noise.decompose()
    parts: list[str] = []
    _collect_text(node, parts)
    return join_lines("".join(parts).splitlines(), limit)


def fragment_text(html: str | None, limit: int = DETAIL_TEXT_LIMIT) -> str:
    return html_to_text(BeautifulSoup(html, "lxml"), limit) if html else ""


def _collect_text(node: Tag, parts: list[str]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if type(child) is NavigableString:
                parts.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        if child.name == "br":
            parts.append("\n")
            continue
        block = child.name in _BLOCK_TAGS
        if block:
            parts.append("\n")
        if child.name == "li":
            parts.append("• ")
        _collect_text(child, parts)
        if block:
            parts.append("\n")


def join_lines(lines: Iterable[str], limit: int = DETAIL_TEXT_LIMIT) -> str:
    cleaned: list[str] = []
    bullet = False
    for line in lines:
        line = _SPACES.sub(" ", line).strip()
        if line == "•":
            bullet = True
            continue
        if not line:
            continue
        if bullet and not line.startswith("•"):
            line = f"• {line}"
        bullet = False
        if line in (",", ";") and cleaned:
            cleaned[-1] += line
            continue
        if cleaned and _continues(cleaned[-1], line):
            cleaned[-1] += " " + line
            continue
        cleaned.append(line)
    return "\n".join(cleaned)[:limit].strip()


def _continues(previous: str, line: str) -> bool:
    """`Location:` + `Belgrade` and `IT,` + `Sales` read better as one line."""
    if line.startswith("•"):
        return False
    if previous.endswith(","):
        return len(previous) < 200
    return previous.endswith(":") and len(previous) <= 30 and len(line) <= 80


def card_text(*lines: str | None) -> str:
    return "\n".join(line.strip() for line in lines if line and line.strip())


def favicon_from_html(html: str, page_url: str) -> str:
    """`<link rel="icon">` of a page, else /favicon.ico on the same host."""
    soup = BeautifulSoup(html, "lxml")
    links = soup.select("link[rel~=icon][href]")
    if links:
        return urljoin(page_url, links[0]["href"])
    parsed = urlparse(page_url)
    return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"


def stop_date(ctx: CollectContext) -> datetime:
    """Items older than this end the walk: the run window or the previous run minus a day of slack."""
    if ctx.cursor.last_published_at is None:
        return ctx.since
    return max(ctx.since, ctx.cursor.last_published_at - timedelta(days=1))


class ListingCrawler:
    """Walks newest-first listings, applies the shared stop rules and dedupes cards by external id."""

    def __init__(self, ctx: CollectContext):
        self.ctx = ctx
        self.truncated = False
        self._items: dict[str, RawItem] = {}
        self._pages = 0
        self._stop_date = stop_date(ctx)

    @property
    def items(self) -> list[RawItem]:
        return list(self._items.values())

    @property
    def limit_reached(self) -> bool:
        return self.ctx.limit is not None and len(self._items) >= self.ctx.limit

    def crawl(self, fetch_page: PageFetcher) -> None:
        """`fetch_page(n)` returns the cards of page n (1-based) and whether a next page exists."""
        for page in range(1, self.ctx.max_pages + 1):
            if self.limit_reached:
                return
            self.ctx.check_cancel()
            self._pages += 1
            self.ctx.on_page(self._pages)
            cards, has_next = fetch_page(page)
            if not self._take(cards) or not has_next:
                return
        self.truncated = True

    def result(self) -> CollectResult:
        status = "truncated" if self.truncated and not self.limit_reached else "ok"
        return CollectResult(items=self.items, status=status, new_cursor=None)

    def _take(self, cards: list[RawItem]) -> bool:
        if not cards or all(self.ctx.is_known(card.external_id) for card in cards):
            return False
        keep_going = True
        for card in cards:
            if card.published_at is not None and card.published_at < self._stop_date:
                keep_going = False
                continue
            if card.external_id in self._items:
                continue
            self._items[card.external_id] = card
            if self.limit_reached:
                return False
        return keep_going
