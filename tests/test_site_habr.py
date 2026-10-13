from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vacancy_retriever.collectors.base import CollectContext, SourceCursor, SourceError
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.habr import HabrCollector, parse_vacancies, parse_vacancy_page
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

HB = SAMPLES / "html" / "habr"
LIST = json.loads((HB / "vacancies_page1.json").read_text(encoding="utf-8"))
DETAIL = (HB / "vacancy_1000167929.html").read_text(encoding="utf-8")
OLD = datetime(2000, 1, 1, tzinfo=UTC)


class _Transport(httpx.BaseTransport):
    """API page N returns the sample with ids shifted by N, so pages differ; /vacancies/<id> is the sample page."""

    def __init__(self) -> None:
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        if not request.url.path.startswith("/api/"):
            return httpx.Response(200, text=DETAIL, headers={"content-type": "text/html"})
        page = int(request.url.params.get("page", "1"))
        shifted = [{**v, "id": v["id"] + (page - 1) * 1000} for v in LIST["list"]]
        return httpx.Response(200, json={**LIST, "list": shifted})


def _collector(transport: httpx.BaseTransport, **params: object) -> HabrCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    source = SourceConfig(id="habr", type="habr", name="Habr Career", url="https://career.habr.com/api/frontend/vacancies",
                          params=params)
    return HabrCollector(source, http)


def _ctx(**kwargs: object) -> CollectContext:
    defaults: dict = {"cursor": SourceCursor(), "since": OLD, "max_pages": 5, "is_known": lambda _id: False}
    return CollectContext(**{**defaults, **kwargs})


def test_parses_vacancies():
    items = parse_vacancies(LIST, "habr")
    assert len(items) == 25
    first = items[0]
    assert first.external_id.isdigit()
    assert first.url == f"https://career.habr.com/vacancies/{first.external_id}"
    assert first.detail_url == first.url
    assert first.title and first.text.startswith(first.title)
    assert first.published_at is not None and first.published_at.utcoffset() == timedelta(0)
    assert first.meta["company"] and first.meta["company"] in first.text
    dates = [i.published_at for i in items]
    assert dates == sorted(dates, reverse=True)
    assert any("Навыки:" in i.text for i in items)


def test_query_uses_params():
    transport = _Transport()
    _collector(transport, qualifications=[3], remote_only=True).collect(_ctx(max_pages=1))
    query = transport.requests[0].params
    assert query.get_list("s[]") == ["43", "44", "76", "125", "129"]
    assert query.get_list("qid[]") == ["3"]
    assert query["sort"] == "date" and query["remote"] == "true" and query["page"] == "1"


def test_max_pages_marks_truncated():
    transport = _Transport()
    result = _collector(transport).collect(_ctx(max_pages=1))
    assert result.status == "truncated"
    assert len(result.items) == 25


def test_last_page_is_ok():
    transport = _Transport()
    result = _collector(transport).collect(_ctx(max_pages=5))
    assert result.status == "ok"
    assert len(transport.requests) == LIST["meta"]["totalPages"]


def test_stops_on_page_of_known_ids():
    known = {i.external_id for i in parse_vacancies(LIST)}
    transport = _Transport()
    result = _collector(transport).collect(_ctx(is_known=lambda i: i in known))
    assert result.items == [] and result.status == "ok"
    assert len(transport.requests) == 1


def test_stops_at_cursor_date_minus_one_day():
    items = parse_vacancies(LIST)
    cursor_date = items[5].published_at + timedelta(days=1)
    transport = _Transport()
    result = _collector(transport).collect(_ctx(cursor=SourceCursor(last_published_at=cursor_date)))
    assert result.status == "ok" and len(transport.requests) == 1
    assert result.items
    assert all(i.published_at >= items[5].published_at for i in result.items)
    assert len(result.items) < len(items)


def test_since_and_limit():
    items = parse_vacancies(LIST)
    result = _collector(_Transport()).collect(_ctx(since=items[0].published_at))
    assert [i.external_id for i in result.items] == [items[0].external_id]
    assert len(_collector(_Transport()).collect(_ctx(limit=4)).items) == 4


def test_fetch_detail_returns_description():
    item = parse_vacancies(LIST)[0]
    text = _collector(_Transport()).fetch_detail(item)
    assert "AGIMA" in text and "Что предстоит делать" in text
    assert "<" not in text and "\n• " in text


def test_detail_without_description_raises():
    with pytest.raises(SourceError):
        parse_vacancy_page("<html><body>nothing</body></html>")
