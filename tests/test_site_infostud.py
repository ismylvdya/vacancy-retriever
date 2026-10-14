from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from vacancy_retriever.collectors.base import CollectContext, SourceCursor, SourceError
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.infostud import InfostudCollector, parse_day, parse_detail, parse_listing
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

INFOSTUD = SAMPLES / "html" / "infostud"
SINCE = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)


def _read(name: str) -> str:
    return (INFOSTUD / name).read_text(encoding="utf-8")


class _Transport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        if request.url.path.startswith("/oglasi-za-posao/"):
            page = request.url.params.get("page", "1")
            body = _read(f"listing_p{page}.html") if page in {"1", "2"} else _read("listing_p2.html")
        else:
            body = _read("detail.html")
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def _collector(transport: httpx.BaseTransport, **params: object) -> InfostudCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    source = SourceConfig(id="infostud", type="infostud", name="Infostud", params=params)
    return InfostudCollector(source, http)


def _ctx(max_pages: int = 5, **kwargs: object) -> CollectContext:
    defaults: dict = {"cursor": SourceCursor(), "since": SINCE, "is_known": lambda _id: False}
    defaults.update(kwargs)
    return CollectContext(max_pages=max_pages, **defaults)


def test_parse_listing_jobs_and_total():
    items, total = parse_listing(_read("listing_p1.html"), "infostud")
    assert len(items) == 30
    assert total == 85
    first = items[0]
    assert first.external_id.isdigit()
    assert first.url.startswith("https://poslovi.infostud.com/posao/")
    assert first.title and first.title in first.text
    assert first.published_at is not None and first.published_at.tzinfo is not None
    assert first.meta["company"]


def test_parse_day_is_end_of_day_and_never_future():
    assert parse_day("06.10.2026", datetime(2026, 10, 14, tzinfo=UTC)) == datetime(2026, 10, 6, 23, 59, tzinfo=UTC)
    assert parse_day("22.10.2026.", datetime(2026, 10, 14, tzinfo=UTC)) == datetime(2026, 10, 14, tzinfo=UTC)
    assert parse_day("2026-10-07", datetime(2026, 10, 14, tzinfo=UTC)).day == 7
    assert parse_day("garbage") is None


def test_parse_detail_has_description():
    text = parse_detail(_read("detail.html"))
    assert text.startswith("AI Developer / Machine Learning Engineer")
    assert "Employment:" in text
    assert len(text) > 500


def test_collect_sends_filters_and_walks_pages():
    transport = _Transport()
    result = _collector(transport, categories=[5, 61], youth=True, dist=50).collect(_ctx(max_pages=3))
    listing = [u for u in transport.requests if u.path.startswith("/oglasi-za-posao/")]
    assert listing[0].path == "/oglasi-za-posao/beograd"
    params = listing[0].params
    assert params["category[0]"] == "5" and params["category[1]"] == "61"
    assert params["youthPosition"] == "1"
    assert params["onlineAfterDate"] == "2026-10-10"
    assert len(listing) == 3  # 85 jobs / 30 per page
    ids = [item.external_id for item in result.items]
    assert len(ids) == len(set(ids))
    assert result.status == "ok"


def test_missing_next_data_is_a_layout_error():
    class Broken(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>nothing</html>", headers={"content-type": "text/html"})

    with pytest.raises(SourceError):
        _collector(Broken()).collect(_ctx())
