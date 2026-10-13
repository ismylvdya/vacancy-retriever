from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.getmatch import GetmatchCollector, fragment_text, parse_offers
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

GM = SAMPLES / "html" / "getmatch"
LIST = json.loads((GM / "offers_page1.json").read_text(encoding="utf-8"))
DETAIL = json.loads((GM / "offer_36200.json").read_text(encoding="utf-8"))
OLD = datetime(2000, 1, 1, tzinfo=UTC)


class _Transport(httpx.BaseTransport):
    """Every listing request returns the sample page; /api/offers/<id> returns the sample offer."""

    def __init__(self) -> None:
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        body = DETAIL if request.url.path.rstrip("/") != "/api/offers" else LIST
        return httpx.Response(200, json=body)


def _collector(transport: httpx.BaseTransport, **params: object) -> GetmatchCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    source = SourceConfig(id="getmatch", type="getmatch", name="getmatch", url="https://getmatch.ru/api/offers",
                          params=params)
    return GetmatchCollector(source, http)


def _ctx(**kwargs: object) -> CollectContext:
    defaults: dict = {"cursor": SourceCursor(), "since": OLD, "max_pages": 5, "is_known": lambda _id: False}
    return CollectContext(**{**defaults, **kwargs})


def test_parses_offers():
    items = parse_offers(LIST, "getmatch")
    assert len(items) == len(LIST["offers"]) == 50
    first = items[0]
    assert first.external_id.isdigit()
    assert first.url.startswith("https://getmatch.ru/vacancies/")
    assert first.title and first.title in first.text
    assert first.published_at is not None and first.published_at.tzinfo is not None
    assert first.detail_url == f"https://getmatch.ru/api/offers/{first.external_id}"
    assert first.meta["company"] and first.meta["company"] in first.text
    assert all(i.source_id == "getmatch" and not i.multi for i in items)
    dates = [i.published_at for i in items]
    assert dates == sorted(dates, reverse=True)
    assert "<b>" not in "".join(i.text for i in items)


def test_query_uses_params():
    transport = _Transport()
    _collector(transport, seniority=["junior", "middle"], page_size=20).collect(_ctx(max_pages=1))
    query = transport.requests[0].params
    assert query.get_list("se") == ["junior", "middle"]
    assert query.get_list("sp") == ["data_engineering", "data_science", "python", "data_analyst_bi"]
    assert query["limit"] == "20" and query["offset"] == "0"


def test_max_pages_marks_truncated_and_pages_by_offset():
    transport = _Transport()
    result = _collector(transport).collect(_ctx(max_pages=2))
    assert result.status == "truncated"
    assert [r.params["offset"] for r in transport.requests] == ["0", "50"]
    assert len(result.items) == 50


def test_stops_on_page_of_known_ids():
    known = {i.external_id for i in parse_offers(LIST)}
    transport = _Transport()
    result = _collector(transport).collect(_ctx(is_known=lambda i: i in known))
    assert result.status == "ok"
    assert result.items == []
    assert len(transport.requests) == 1


def test_stops_at_cursor_date_minus_one_day():
    items = parse_offers(LIST)
    cursor_date = items[10].published_at
    transport = _Transport()
    result = _collector(transport).collect(_ctx(cursor=SourceCursor(last_published_at=cursor_date)))
    assert result.status == "ok"
    assert len(transport.requests) == 1
    assert result.items
    assert all(i.published_at >= cursor_date - timedelta(days=1) for i in result.items)
    assert len(result.items) < len(items)


def test_since_and_limit():
    items = parse_offers(LIST)
    newest = items[0].published_at
    result = _collector(_Transport()).collect(_ctx(since=newest - timedelta(seconds=1)))
    assert [i.external_id for i in result.items] == [items[0].external_id]
    limited = _collector(_Transport()).collect(_ctx(limit=3))
    assert len(limited.items) == 3 and limited.status == "ok"


def test_fetch_detail_returns_plain_text():
    collector = _collector(_Transport())
    item = parse_offers(LIST)[0]
    text = collector.fetch_detail(item)
    assert text and "Quantitative Researcher" in text
    assert "Задачи" in text and "<" not in text


def test_fragment_text_keeps_lines():
    assert fragment_text("<p>a <b>b</b></p><ul><li>x</li><li>y</li></ul>") == "a b\n• x\n• y"
