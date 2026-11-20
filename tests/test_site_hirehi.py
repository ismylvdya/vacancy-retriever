"""hirehi.ru: listing cards, pages of a filter link with repeated keys, the JobPosting of a vacancy page."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.hirehi import HirehiCollector, page_url, parse_listing, parse_vacancy_page
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

HH = SAMPLES / "html" / "hirehi"
LISTING = (HH / "listing_data_analyst.html").read_text(encoding="utf-8")
DETAIL = (HH / "vacancy_252420.html").read_text(encoding="utf-8")
FILTER = "https://hirehi.ru/vacancies/data-analyst?format=office&format=remote_russia&level=junior&country=Russia"


def test_listing_cards():
    items, has_next = parse_listing(LISTING, "hirehi")
    assert len(items) > 20 and not has_next
    first = items[0]
    assert first.external_id.isdigit() and first.url.startswith("https://hirehi.ru/")
    assert first.title and first.published_at is not None and first.published_at.tzinfo is not None
    assert first.detail_url == first.url


def test_page_url_keeps_repeated_filter_keys():
    assert page_url(FILTER, 1) == FILTER
    assert page_url(FILTER, 2) == FILTER + "&page=2"


def test_vacancy_page_description():
    text = parse_vacancy_page(DETAIL)
    assert text.startswith("Аналитик данных") and "Компания:" in text and "Требования" in text


class _Transport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.urls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(200, text=LISTING, headers={"content-type": "text/html"})


def test_collector_walks_every_listing_and_dedupes():
    transport = _Transport()
    source = SourceConfig(id="hirehi", type="hirehi", name="HireHi", max_pages=3,
                          params={"listings": [FILTER, FILTER.replace("data-analyst", "ml-ai")]})
    collector = HirehiCollector(source, PoliteHttpClient(min_delay=0, max_delay=0, transport=transport))
    ctx = CollectContext(SourceCursor(), datetime(2000, 1, 1, tzinfo=UTC), 3, lambda _id: False)
    result = collector.collect(ctx)
    assert len(result.items) == len(parse_listing(LISTING)[0])
    assert len(transport.urls) == 2 and transport.urls[0].count("format=") == 2
