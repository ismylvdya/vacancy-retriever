from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vacancy_retriever.collectors.base import CollectContext, RawItem, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.ovdejobs import OvdeJobsCollector, parse_dates, parse_detail, parse_listing
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

OVDE = SAMPLES / "html" / "ovdejobs"
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
EMPTY_PAGE = '<html><div class="facetwp-template"></div><script>var FWP_JSON = {"pager":{"total_pages":3}};</script></html>'


def _read(name: str) -> str:
    return (OVDE / name).read_text(encoding="utf-8")


def _all_dates() -> list[dict]:
    return [record for name in ("belgrade_p1", "belgrade_p2", "remote_p1") for record in json.loads(_read(f"dates_{name}.json"))]


class _OvdeTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append(url)
        if url.path.startswith("/wp-json/"):
            wanted = set(url.params["include"].split(","))
            return httpx.Response(200, json=[r for r in _all_dates() if str(r["id"]) in wanted])
        if url.path == "/vacancies/":
            return self._html(self._listing(url.params.get("_job_city"), url.params.get("_paged", "1")))
        return self._html(_read("detail.html"))

    @staticmethod
    def _listing(city: str | None, page: str) -> str:
        known = {("belgrade", "1"), ("belgrade", "2"), ("remote", "1")}
        return _read(f"listing_{city}_p{page}.html") if (city, page) in known else EMPTY_PAGE

    @staticmethod
    def _html(body: str) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    def listing_pages(self) -> list[tuple[str | None, str]]:
        return [(u.params.get("_job_city"), u.params.get("_paged", "1")) for u in self.requests if u.path == "/vacancies/"]


def _collector(transport: httpx.BaseTransport, **params: object) -> OvdeJobsCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    source = SourceConfig(id="ovdejobs", type="ovdejobs", name="Ovde Jobs", params=params)
    return OvdeJobsCollector(source, http)


def _ctx(max_pages: int = 5, **kwargs: object) -> CollectContext:
    defaults: dict = {"cursor": SourceCursor(), "since": LONG_AGO, "is_known": lambda _id: False}
    defaults.update(kwargs)
    return CollectContext(max_pages=max_pages, **defaults)


def _page_ids(name: str) -> list[str]:
    cards, _ = parse_listing(_read(f"listing_{name}.html"))
    return [card.job_id for card in cards]


def test_parse_listing_cards_and_pager():
    cards, total_pages = parse_listing(_read("listing_belgrade_p1.html"))
    assert len(cards) == 30
    assert total_pages == 3
    first = cards[0]
    assert first.job_id.isdigit()
    assert first.url.startswith("https://ovdejobs.com/jobs/")
    assert first.title and first.company and first.city


def test_parse_dates_are_utc():
    dates = parse_dates(_all_dates())
    assert dates["181139"] == datetime(2026, 10, 13, 14, 0, 55, tzinfo=UTC)


def test_collects_all_cities_without_duplicates():
    transport = _OvdeTransport()
    result = _collector(transport).collect(_ctx())
    ids = [item.external_id for item in result.items]
    expected = set(_page_ids("belgrade_p1")) | set(_page_ids("belgrade_p2")) | set(_page_ids("remote_p1"))
    assert len(ids) == len(set(ids)) == len(expected)
    assert set(ids) == expected
    assert result.status == "ok"
    item = result.items[0]
    assert item.source_id == "ovdejobs"
    assert item.published_at is not None and item.published_at.tzinfo is not None
    assert item.detail_url == item.url
    assert item.meta["company"] and item.title in item.text
    assert all(i.published_at for i in result.items)


def test_max_pages_marks_truncated():
    transport = _OvdeTransport()
    result = _collector(transport).collect(_ctx(max_pages=1))
    assert result.status == "truncated"
    assert ("belgrade", "2") not in transport.listing_pages()


def test_stops_on_page_of_known_ids():
    known = set(_page_ids("belgrade_p1"))
    transport = _OvdeTransport()
    result = _collector(transport, cities=["belgrade"]).collect(_ctx(is_known=known.__contains__))
    assert result.items == []
    assert result.status == "ok"
    assert transport.listing_pages() == [("belgrade", "1")]


def test_stops_at_cursor_date_minus_one_day():
    dates = parse_dates(_all_dates())
    cursor_at = dates[_page_ids("belgrade_p1")[5]] + timedelta(days=1)
    transport = _OvdeTransport()
    result = _collector(transport, cities=["belgrade"]).collect(_ctx(cursor=SourceCursor(last_published_at=cursor_at)))
    assert result.status == "ok"
    assert result.items
    assert all(i.published_at >= cursor_at - timedelta(days=1) for i in result.items)
    assert transport.listing_pages() == [("belgrade", "1")]


def test_skips_items_older_than_since():
    since = datetime(2026, 10, 10, tzinfo=UTC)
    result = _collector(_OvdeTransport()).collect(_ctx(since=since))
    assert result.items
    assert all(i.published_at >= since for i in result.items)


def test_limit_caps_items():
    result = _collector(_OvdeTransport()).collect(_ctx(limit=5))
    assert len(result.items) == 5
    assert result.status == "ok"


def test_category_filter_is_sent_per_category():
    transport = _OvdeTransport()
    _collector(transport, cities=["belgrade"], categories=["it", "construction"]).collect(_ctx(max_pages=1))
    categories = [u.params.get("_job_category") for u in transport.requests if u.path == "/vacancies/"]
    assert categories == ["it", "construction"]


def test_parse_detail_keeps_description_only():
    text = parse_detail(_read("detail.html"))
    assert text.startswith("Purchasing manager")
    assert "Location: Belgrade" in text
    assert "Requirements" in text and "• " in text
    assert "Easy Apply" not in text and "<" not in text
    assert len(text) <= 8000


def test_fetch_detail_and_icon():
    collector = _collector(_OvdeTransport())
    item = RawItem("ovdejobs", "181083", "https://ovdejobs.com/jobs/purchasing-manager-11/", "", None,
                   detail_url="https://ovdejobs.com/jobs/purchasing-manager-11/")
    assert "Purchasing manager" in collector.fetch_detail(item)
    icon = collector.icon_url()
    assert icon and icon.startswith("https://ovdejobs.com/") and "favicon" not in icon


@pytest.mark.parametrize("name", ["listing_belgrade_p1.html", "detail.html"])
def test_samples_have_no_personal_contacts(name: str):
    emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", _read(name)))
    assert emails <= {"hidden@example.com"}
