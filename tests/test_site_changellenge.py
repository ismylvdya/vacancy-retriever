from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.changellenge import (
    DEFAULT_LISTING,
    ChangellengeCollector,
    parse_detail,
    parse_listing,
)
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

DIR = SAMPLES / "html" / "changellenge"
ALL_LISTING = "https://changellenge.com/vacancy/"


def _html(name: str) -> str:
    return (DIR / name).read_text(encoding="utf-8")


class _SiteTransport(httpx.BaseTransport):
    """Filtered listing = one page; full listing serves the same page for every PAGEN_1."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url.startswith(DEFAULT_LISTING):
            body = _html("listing.html")
        elif url.startswith(ALL_LISTING + "?") or url == ALL_LISTING:
            body = _html("listing_all.html")
        else:
            body = _html("detail.html")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def _collector(transport: httpx.BaseTransport, **source: object) -> ChangellengeCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    return ChangellengeCollector(SourceConfig(id="changellenge", type="changellenge", name="x", **source), http)


def _ctx(max_pages: int = 3, known: set[str] | None = None, limit: int | None = None) -> CollectContext:
    known = known or set()
    return CollectContext(SourceCursor(), datetime(2000, 1, 1, tzinfo=UTC), max_pages, known.__contains__, limit=limit)


def test_parses_cards():
    items, nav = parse_listing(_html("listing.html"))
    assert len(items) == 7
    assert nav is None
    first = items[0]
    assert first.external_id.isdigit()
    assert first.url.startswith("https://changellenge.com/vacancy/")
    assert first.detail_url == first.url
    assert first.title and first.title in first.text
    assert first.meta["location"]
    assert all(i.published_at is None for i in items)


def test_full_listing_has_load_more():
    items, nav = parse_listing(_html("listing_all.html"))
    assert items and nav == "PAGEN_1"


def test_single_page_listing_is_ok():
    transport = _SiteTransport()
    result = _collector(transport).collect(_ctx())
    assert result.status == "ok"
    assert len(result.items) == 7
    assert all(i.source_id == "changellenge" for i in result.items)
    assert len(transport.requests) == 1


def test_repeated_page_stops_paging():
    transport = _SiteTransport()
    result = _collector(transport, url=ALL_LISTING).collect(_ctx(max_pages=5))
    assert result.status == "ok"
    assert transport.requests == [ALL_LISTING, ALL_LISTING + "?PAGEN_1=2"]


def test_max_pages_marks_truncated():
    result = _collector(_SiteTransport(), url=ALL_LISTING).collect(_ctx(max_pages=1))
    assert result.status == "truncated"


def test_known_page_stops():
    items, _ = parse_listing(_html("listing_all.html"))
    transport = _SiteTransport()
    result = _collector(transport, url=ALL_LISTING).collect(_ctx(known={i.external_id for i in items}))
    assert result.items == []
    assert result.status == "ok"
    assert len(transport.requests) == 1


def test_limit():
    result = _collector(_SiteTransport()).collect(_ctx(limit=3))
    assert len(result.items) == 3


def test_detail_text():
    text = parse_detail(_html("detail.html"))
    assert text.startswith("Data Scientist в команду LLM")
    assert "Компания: Авито" in text
    assert "• " in text and "Обязанности" in text
    assert "<" not in text


def test_fetch_detail_uses_detail_url():
    transport = _SiteTransport()
    collector = _collector(transport)
    item = parse_listing(_html("listing.html"))[0][0]
    assert "Требования" in collector.fetch_detail(item)
    assert transport.requests == [item.detail_url]
