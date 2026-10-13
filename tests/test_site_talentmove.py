from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.talentmove import TalentMoveCollector, parse_detail, parse_feed
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

DIR = SAMPLES / "html" / "talentmove"
FEED = (DIR / "feed.xml").read_text(encoding="utf-8")
DETAIL = (DIR / "detail.html").read_text(encoding="utf-8")
OLD = datetime(2000, 1, 1, tzinfo=UTC)


class _SiteTransport(httpx.BaseTransport):
    """Feed pages up to `last_page` (post ids shifted per page so pages differ), 404 after; any other URL is a job."""

    def __init__(self, last_page: int = 99) -> None:
        self.last_page = last_page
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        if not request.url.path.startswith("/jobs/feed"):
            return httpx.Response(200, text=DETAIL, headers={"content-type": "text/html"})
        page = int(request.url.params.get("paged", "1"))
        if page > self.last_page:
            return httpx.Response(404, text="<html>not found</html>", headers={"content-type": "text/html"})
        body = re.sub(r"-(\d+)/</link>", lambda m: f"-{int(m.group(1)) + page * 1_000_000}/</link>", FEED)
        return httpx.Response(200, text=body, headers={"content-type": "application/rss+xml"})


def _collector(transport: httpx.BaseTransport, **params: object) -> TalentMoveCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    return TalentMoveCollector(SourceConfig(id="talentmove", type="talentmove", name="x", params=params), http)


def _ctx(max_pages: int = 3, cursor: SourceCursor | None = None, since: datetime = OLD,
         known: set[str] | None = None, limit: int | None = None) -> CollectContext:
    known = known or set()
    return CollectContext(cursor or SourceCursor(), since, max_pages, known.__contains__, limit=limit)


def test_parses_feed():
    items = parse_feed(FEED)
    assert len(items) == 10
    first = items[0]
    assert first.external_id.isdigit()
    assert first.url.startswith("https://talent-move.ru/jobs/") and first.url.endswith(f"-{first.external_id}/")
    assert first.detail_url == first.url
    assert first.text.startswith(first.title)
    assert "<" not in first.text and "• " in "\n".join(i.text for i in items)
    assert first.published_at.tzinfo is not None
    dates = [i.published_at for i in items]
    assert dates == sorted(dates, reverse=True)


def test_requests_filters_and_pages():
    transport = _SiteTransport()
    result = _collector(transport).collect(_ctx(max_pages=2))
    assert result.status == "truncated"
    assert len(result.items) == 20
    first, second = transport.requests
    assert first.params["seniority"] == "junior" and first.params["job_category"] == "data,analytics,ai"
    assert "paged" not in first.params and second.params["paged"] == "2"
    assert all(i.source_id == "talentmove" for i in result.items)


def test_custom_filters():
    transport = _SiteTransport(last_page=1)
    _collector(transport, filters={"job_category": "ai"}).collect(_ctx())
    assert dict(transport.requests[0].params) == {"job_category": "ai"}


def test_404_after_last_page_is_ok():
    result = _collector(_SiteTransport(last_page=1)).collect(_ctx())
    assert result.status == "ok" and len(result.items) == 10


def test_404_on_first_page_raises():
    with pytest.raises(httpx.HTTPStatusError):
        _collector(_SiteTransport(last_page=0)).collect(_ctx())


def test_known_page_stops():
    known = {str(int(i.external_id) + 1_000_000) for i in parse_feed(FEED)}
    transport = _SiteTransport()
    result = _collector(transport).collect(_ctx(known=known))
    assert result.items == [] and len(transport.requests) == 1


def test_cursor_date_stops_with_one_day_slack():
    items = parse_feed(FEED)
    cursor = SourceCursor(last_published_at=items[3].published_at + timedelta(days=1))
    result = _collector(_SiteTransport()).collect(_ctx(cursor=cursor))
    assert result.status == "ok"
    assert len(result.items) == 4


def test_since_stops():
    items = parse_feed(FEED)
    result = _collector(_SiteTransport()).collect(_ctx(since=items[1].published_at))
    assert result.status == "ok" and len(result.items) == 2


def test_limit():
    assert len(_collector(_SiteTransport()).collect(_ctx(limit=3)).items) == 3


def test_detail_text():
    text = parse_detail(DETAIL)
    assert text.startswith("Junior+ Продуктовый Дизайнер")
    assert "Компания: Фоксфорд" in text
    assert "• Проектировать" in text
    assert "Оценка вакансии" not in text and "Подписка" not in text


def test_fetch_detail():
    transport = _SiteTransport()
    item = parse_feed(FEED)[0]
    assert "Компания:" in _collector(transport).fetch_detail(item)
    assert str(transport.requests[0]) == item.detail_url
