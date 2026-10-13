from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vacancy_retriever.collectors.base import CollectContext, SourceCursor, SourceError
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.geekjob import (
    GeekjobCollector,
    parse_ru_date,
    parse_vacancies,
    parse_vacancy_page,
)
from vacancy_retriever.config import SourceConfig

from .conftest import NOW, SAMPLES

GJ = SAMPLES / "html" / "geekjob"
LIST = json.loads((GJ / "vacancies_page1.json").read_text(encoding="utf-8"))
DETAIL = (GJ / "vacancy_6ac61c452099dfc37c0f7ec0.html").read_text(encoding="utf-8")
OLD = datetime(2000, 1, 1, tzinfo=UTC)


class _Transport(httpx.BaseTransport):
    """Endless listing: page N is the sample with ids suffixed by N; /vacancy/<id> is the sample page."""

    def __init__(self) -> None:
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        if request.url.path.startswith("/vacancy/"):
            return httpx.Response(200, text=DETAIL, headers={"content-type": "text/html"})
        page = int(request.url.params["page"])
        data = [{**v, "id": f"{v['id']}-{page}"} for v in LIST["data"]]
        return httpx.Response(200, json={**LIST, "page": page, "nextpage": page + 1, "data": data})


def _collector(transport: httpx.BaseTransport, **params: object) -> GeekjobCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    source = SourceConfig(id="geekjob", type="geekjob", name="GeekJob", url="https://geekjob.ru/json/find/vacancy",
                          params=params)
    collector = GeekjobCollector(source, http)
    collector.now = lambda: NOW
    return collector


def _ctx(**kwargs: object) -> CollectContext:
    defaults: dict = {"cursor": SourceCursor(), "since": OLD, "max_pages": 3, "is_known": lambda _id: False}
    return CollectContext(**{**defaults, **kwargs})


def test_parse_ru_date():
    assert parse_ru_date("7 октября", NOW) == datetime(2026, 10, 7, tzinfo=UTC)
    assert parse_ru_date("18 мая", NOW) == datetime(2026, 5, 18, tzinfo=UTC)
    assert parse_ru_date("20 декабря", NOW) == datetime(2025, 12, 20, tzinfo=UTC)
    assert parse_ru_date("3 марта 2024", NOW) == datetime(2024, 3, 3, tzinfo=UTC)
    assert parse_ru_date("сегодня", NOW) == datetime(2026, 10, 13, tzinfo=UTC)
    assert parse_ru_date("вчера", NOW) == datetime(2026, 10, 12, tzinfo=UTC)
    assert parse_ru_date("", NOW) is None


def test_parses_vacancies():
    items = parse_vacancies(LIST, NOW, "geekjob")
    assert len(items) == len(LIST["data"]) == 19
    first = items[0]
    assert first.url == f"https://geekjob.ru/vacancy/{first.external_id}"
    assert first.detail_url == first.url
    assert first.title == "Senior Data Engineer"
    assert first.published_at == datetime(2026, 10, 7, tzinfo=UTC)
    assert "Компания: Martsun & Partners" in first.text and "удалённо" in first.text
    assert "Зарплата:" in first.text
    assert first.meta["company"] == "Martsun & Partners"
    assert any(i.meta["location"] and "Москва" in i.meta["location"] for i in items)


def test_query_uses_params():
    transport = _Transport()
    _collector(transport, tags=[52, 277], relocate=True).collect(_ctx(max_pages=1))
    query = transport.requests[0].params
    assert query["t"] == "52,277" and query["rm"] == "1" and query["rl"] == "1" and query["page"] == "1"


def test_max_pages_marks_truncated():
    transport = _Transport()
    result = _collector(transport).collect(_ctx(max_pages=2))
    assert result.status == "truncated"
    assert len(transport.requests) == 2
    assert len(result.items) == 38


def test_single_page_listing_is_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=LIST)

    result = _collector(httpx.MockTransport(handler)).collect(_ctx())
    assert result.status == "ok" and len(result.items) == 19


def test_stops_on_page_of_known_ids():
    transport = _Transport()
    result = _collector(transport).collect(_ctx(is_known=lambda i: i.endswith("-1")))
    assert result.items == [] and result.status == "ok"
    assert len(transport.requests) == 1


def test_stops_at_cursor_date_minus_one_day():
    transport = _Transport()
    cursor = SourceCursor(last_published_at=datetime(2026, 10, 7, tzinfo=UTC))
    result = _collector(transport).collect(_ctx(cursor=cursor))
    assert result.status == "ok" and len(transport.requests) == 1
    assert {i.published_at.day for i in result.items} == {6, 7}


def test_since_and_limit():
    result = _collector(_Transport()).collect(_ctx(since=datetime(2026, 10, 7, tzinfo=UTC)))
    assert len(result.items) == 3 and result.status == "ok"
    assert len(_collector(_Transport()).collect(_ctx(limit=5)).items) == 5


def test_fetch_detail_returns_description():
    item = parse_vacancies(LIST, NOW)[0]
    text = _collector(_Transport()).fetch_detail(item)
    assert text.startswith("Уровень: Сеньор")
    assert "Palantir Foundry" in text and "Удаленная работа" in text
    assert "<" not in text


def test_detail_without_description_raises():
    with pytest.raises(SourceError):
        parse_vacancy_page("<html><body><article class='vacancy'></article></body></html>")


def test_dates_relative_to_collect_time():
    items = parse_vacancies(LIST, NOW - timedelta(days=200))
    assert items[0].published_at.year == 2025
