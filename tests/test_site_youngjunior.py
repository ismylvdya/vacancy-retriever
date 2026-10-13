from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.youngjunior import YoungJuniorCollector, parse_vacancies
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

PAYLOAD: dict[str, Any] = json.loads((SAMPLES / "html" / "youngjunior" / "vacancies.json").read_text(encoding="utf-8"))
OLD = datetime(2000, 1, 1, tzinfo=UTC)


class _ApiTransport(httpx.BaseTransport):
    """Every page returns the sample payload with ids shifted by `skip`, so pages differ."""

    def __init__(self, has_more: bool = True) -> None:
        self.has_more = has_more
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        skip = int(request.url.params["skip"])
        items = [{**item, "id": item["id"] + skip * 1000} for item in PAYLOAD["items"]]
        return httpx.Response(200, json={**PAYLOAD, "items": items, "has_more": self.has_more})


def _collector(transport: httpx.BaseTransport, **params: object) -> YoungJuniorCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    source = SourceConfig(id="youngjunior", type="youngjunior", name="x", params={"page_size": 10, **params})
    return YoungJuniorCollector(source, http)


def _ctx(max_pages: int = 3, cursor: SourceCursor | None = None, since: datetime = OLD,
         known: set[str] | None = None, limit: int | None = None) -> CollectContext:
    known = known or set()
    return CollectContext(cursor or SourceCursor(), since, max_pages, known.__contains__, limit=limit)


def _sample_items():
    return parse_vacancies(PAYLOAD)[0]


def test_parses_items():
    items, has_more = parse_vacancies(PAYLOAD)
    assert has_more
    assert len(items) == 10
    first = items[0]
    assert first.external_id == str(PAYLOAD["items"][0]["id"])
    assert first.url == f"https://youngjunior.ru/data/jobs/{first.external_id}"
    assert first.title and first.text.startswith(first.title)
    assert "Компания: " in first.text and first.meta["company"]
    assert first.published_at.tzinfo is not None
    assert first.detail_url is None
    assert "<" not in first.text and len(first.text) > 300
    dates = [i.published_at for i in items]
    assert dates == sorted(dates, reverse=True)


def test_query_defaults_and_overrides():
    collector = _collector(_ApiTransport())
    query = collector.query(0, 20)
    assert query["speciality"] == "ml_engineer,data_scientist,analyst_data"
    assert query["remote"] == "true"
    assert "remote" not in _collector(_ApiTransport(), remote=False).query(0, 20)


def test_max_pages_marks_truncated():
    transport = _ApiTransport()
    result = _collector(transport).collect(_ctx(max_pages=2))
    assert result.status == "truncated"
    assert len(result.items) == 20
    assert [r.params["skip"] for r in transport.requests] == ["0", "10"]
    assert all(i.source_id == "youngjunior" for i in result.items)


def test_last_page_is_ok():
    result = _collector(_ApiTransport(has_more=False)).collect(_ctx())
    assert result.status == "ok"
    assert len(result.items) == 10


def test_known_page_stops():
    known = {i.external_id for i in _sample_items()}
    transport = _ApiTransport()
    result = _collector(transport).collect(_ctx(known=known))
    assert result.items == [] and result.status == "ok"
    assert len(transport.requests) == 1


def test_cursor_date_stops_with_one_day_slack():
    items = _sample_items()
    cursor_date = items[4].published_at + timedelta(days=1)
    result = _collector(_ApiTransport()).collect(_ctx(cursor=SourceCursor(last_published_at=cursor_date)))
    assert result.status == "ok"
    assert [i.external_id for i in result.items] == [i.external_id for i in items[:5]]


def test_since_stops():
    items = _sample_items()
    result = _collector(_ApiTransport()).collect(_ctx(since=items[2].published_at))
    assert result.status == "ok"
    assert len(result.items) == 3


def test_limit():
    result = _collector(_ApiTransport()).collect(_ctx(limit=4))
    assert len(result.items) == 4 and result.status == "ok"


def test_fetch_detail_not_needed():
    collector = _collector(_ApiTransport())
    assert collector.fetch_detail(_sample_items()[0]) is None
